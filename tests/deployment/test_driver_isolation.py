"""第 3 层 · 驱动侧隔离（manifest 61-64、71-72）。

前面几组验的是"我们这半边"：调度、沙盒、eBPF 拦 `open("/dev/davinciN")`。这一组
验**最终结果**：Ascend 驱动给每个 mount namespace 建的 UDA 设备表里有几张卡 ——
也就是容器和宿主 shell 真正能用几张。两者会分叉：

  * 驱动把容器判成 **admin**（`uda_is_admin_task`：init user ns + effective 覆盖
    `ka_system_get_privileged_kernel_cap()` 的掩码 = bits 0..37）时，它按
    `UDA_MAX_PHY_DEV_NUM` 建出一张**全量**表，而且这条路径不看我们的 eBPF ——
    隔离静默失效（真机复现过：申请 2 张卡的容器里 `torch.npu.device_count()==8`）。
    `neu-box-runtime` 的 cap 守卫（剪 `CAP_AUDIT_READ`）就是为了把这类容器踢出
    admin 分支 —— 61/64/71 是它的真机回归。
  * **没登记**的容器（不带 `sandbox_cgroup` annotation）必须一张都拿不到。

判据取宿主的 `/proc/uda/namespace_node`（驱动自己的账）：每行一个 namespace 节点，
`root_tgid` 是它所属进程的**宿主 PID**，`dev_num`/`udev list` 就是表内容。
别拿 `namespace 00000000xxxxxxxx` 字段去对 inum —— 驱动打的是 %pK（哈希过的
指针），和 `readlink /proc/<pid>/ns/mnt` 不是一个数；按 `root_tgid` 对最稳。

表是在**该 namespace 里第一个进程真正初始化驱动**时建的，之后按 ns 缓存，所以
容器里必须先跑一次设备访问（下面用"逐个 open davinciN + npu-smi"来触发）。
"""

from __future__ import annotations

import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time

import pytest

from deployment_support import container_device_args

_NAMESPACE_NODE = "/proc/uda/namespace_node"
_NPU_SMI = "/usr/local/bin/npu-smi"
_ASCEND_DRIVER = "/usr/local/Ascend/driver"
_ASCEND_INFO = "/etc/ascend_install.info"
_AUX_DEVICES = ["/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc"]

# 容器侧的探测：先把每张 davinci 节点试开一遍（这是"这个 ns 占用了哪些卡"的
# 来源，也是 eBPF 真正拦的那一步），再跑 npu-smi 让驱动把表建出来。
_PROBE = (
    "for n in 0 1 2 3 4 5 6 7; do (exec 3</dev/davinci$n) 2>/dev/null "
    "&& echo OPEN_$n; done; npu-smi info >/dev/null 2>&1; sleep 600"
)

# 宿主侧触发驱动建 UDA 节点，得让进程真的"取到"一张 UDA 设备：`npu-smi info`
# 走的是 DCMI/manager，**不建** /proc/uda 的 ns 节点（真机跑出来过），真正会建的
# 是拿设备做计算的进程（torch）。所以按顺序试下面几条，全失败就报前置缺失，
# 并提示用 NEU_BOX_DEVICE_PROBE_CMD 指定本机可用的那条命令。
_HOST_PROBE_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("python3", "-c", "import torch, torch_npu; print(torch.npu.device_count())"),
    ("python", "-c", "import torch, torch_npu; print(torch.npu.device_count())"),
)

# 本机的 torch_npu 往往不在 root 的 PATH 上（装在某个 conda 环境里）。按绝对路径
# 再捞一遍：找不到就报前置缺失并提示 NEU_BOX_DEVICE_PROBE_CMD，别让人对着
# "python3 里没有 torch_npu" 猜。
_EXTRA_PYTHON_GLOBS = (
    "/home/*/miniconda3/bin/python",
    "/home/*/miniconda3/envs/*/bin/python",
    "/home/*/anaconda3/bin/python",
    "/home/*/anaconda3/envs/*/bin/python",
    "/opt/conda/bin/python",
)

# 探针曾经跑不起来的真实原因：套件是 `sudo neuboxctl test` 起的，环境是 root 的
# 最小集，而 `import torch_npu` 需要 CANN 的 PYTHONPATH / LD_LIBRARY_PATH 和几个
# ASCEND_* 指向（用户交互 shell 里这些由 CANN 装机脚本写进 profile，root 这边没
# 有）。不补就是"本机没有能触发驱动建 UDA 节点的命令"这种假前置缺失 —— 真机上
# 就是这么被卡住的。
_PROBE_FAILURES: list[str] = []


def _cann_env() -> dict:
    """给宿主探针补齐 CANN 的环境变量（找不到 CANN 就返回空）。

    选哪个 CANN 目录：先认现场已经指好的 ``ASCEND_HOME_PATH``，否则优先正式版
    （``cann-9.0.0`` 这种），最后才轮到 ``-beta`` / ``-rc`` 之类的预发布目录 ——
    这台机器上两者都在，正式版才是被 profile 指过去的那个。
    """
    roots = sorted(
        path for path in glob.glob("/usr/local/Ascend/cann-*")
        if os.path.isdir(path)
    )
    if not roots:
        return {}
    configured = os.environ.get("ASCEND_HOME_PATH", "").strip()
    cann = ""
    if configured and os.path.isdir(configured):
        cann = configured
    else:
        release = [
            path for path in roots
            if not any(tag in os.path.basename(path)
                       for tag in ("-beta", "-rc", "-alpha"))
        ]
        cann = (release or roots)[-1]
    driver = "/usr/local/Ascend/driver"
    libraries = [
        f"{cann}/lib64",
        f"{driver}/lib64",
        f"{driver}/lib64/driver",
        f"{driver}/lib64/common",
    ]
    modules = [
        f"{cann}/python/site-packages",
        f"{cann}/opp/built-in/op_impl/ai_core/tbe",
    ]
    environment = {
        "ASCEND_HOME_PATH": cann,
        "ASCEND_TOOLKIT_HOME": cann,
        "ASCEND_AICPU_PATH": cann,
        "ASCEND_OPP_PATH": f"{cann}/opp",
    }
    for key, extra in (("LD_LIBRARY_PATH", libraries), ("PYTHONPATH", modules)):
        existing = [item for item in (os.environ.get(key) or "").split(":") if item]
        environment[key] = ":".join(
            [item for item in extra if item not in existing] + existing
        )
    return environment


def _host_probe_commands() -> list[list[str]]:
    override = os.environ.get("NEU_BOX_DEVICE_PROBE_CMD", "").strip()
    commands = [override.split()] if override else []
    # 只挑真的存在的可执行文件：``python`` 这种名字在这台机器上可能压根没有，
    # 直接拿去 Popen 会抛 FileNotFoundError —— 那是崩溃，不是"这条命令不行，
    # 换下一条"，会把后面的候选（比如 conda 里的 python）一起挡掉。
    commands += [
        [path, *candidate[1:]]
        for candidate in _HOST_PROBE_CANDIDATES
        for path in [shutil.which(candidate[0])]
        if path
    ]
    for pattern in _EXTRA_PYTHON_GLOBS:
        for path in sorted(glob.glob(pattern)):
            if os.access(path, os.X_OK):
                commands.append([
                    path, "-c",
                    "import torch, torch_npu; print(torch.npu.device_count())",
                ])
    return commands


def _start_host_probe() -> subprocess.Popen | None:
    """起一个"会去初始化驱动"的宿主进程（不用 npu-smi，见上面的说明）。

    起不来的候选把原因记进 ``_PROBE_FAILURES``：全失败时调用方要把它贴出来，
    否则现场只能看到"本机没有能触发驱动建 UDA 节点的命令"这种没有信息量的结论。
    """
    del _PROBE_FAILURES[:]
    environment = {**os.environ, **_cann_env()}
    for argv in _host_probe_commands():
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, env=environment,
            )
        except OSError as exc:
            # 找不到/起不来的候选直接跳过，别让"这台机器没有 python"这种前置
            # 把用例打成崩溃。
            _PROBE_FAILURES.append(f"{argv[0]}: 起不来（{exc}）")
            continue
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if _namespace_rows_for_pid(process.pid):
                return process
            if process.poll() is not None:
                # 这条命令跑不通（比如没有 torch_npu、缺 CANN 环境），试下一条。
                reason = ""
                try:
                    tail = (process.communicate(timeout=5)[1] or "").strip()
                    if tail:
                        reason = "，最后一行: " + tail.splitlines()[-1][:160]
                except Exception:  # noqa: BLE001 - 诊断信息不值得再抛
                    reason = ""
                _PROBE_FAILURES.append(
                    f"{' '.join(argv[:2])}: 退出码 {process.returncode}{reason}"
                )
                break
            time.sleep(0.25)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
            _PROBE_FAILURES.append(
                f"{' '.join(argv[:2])}: 20s 内没有建出 UDA 节点"
            )
    return None


def _namespace_rows_for_pid(pid: int) -> list[dict]:
    return [row for row in _namespace_rows() if row['root_tgid'] == pid]


def _namespace_rows() -> list[dict]:
    """读驱动账本；读不到按前置缺失失败（这一组只在装了驱动的机器上有意义）。"""
    try:
        with open(_NAMESPACE_NODE, encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except OSError as exc:
        pytest.fail(
            f"前置缺失：读不到 {_NAMESPACE_NODE}（{exc}）。这一组验的是 Ascend 驱动"
            f"按 mount namespace 建的 UDA 设备表，没有驱动就无从验起",
            pytrace=False,
        )
    rows: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        header = re.match(
            r"\s*ns_id\s+(\d+)\s+identify\s+(\S+)\s+root_tgid\s+(\d+)\s+dev_num\s+(\d+)",
            line,
        )
        if header:
            current = {
                'ns_id': int(header.group(1)),
                'root_tgid': int(header.group(3)),
                'dev_num': int(header.group(4)),
                'udevids': [],
            }
            rows.append(current)
            continue
        if current is None:
            continue
        entry = re.match(r"\s*(\d+)\s+(\d+)\s*$", line)
        if entry:
            current['udevids'].append(int(entry.group(2)))
    return rows


def _wait_row(pid: int, *, timeout: float = 30.0) -> dict:
    """等驱动给这个 PID 的 namespace 建出节点。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in _namespace_rows():
            if row['root_tgid'] == pid:
                return row
        time.sleep(0.25)
    pytest.fail(
        f"{_NAMESPACE_NODE} 里 {timeout:.0f}s 内没有 root_tgid={pid} 的节点："
        f"这个 ns 里的进程没有真正初始化驱动（探测命令没起来，或者 npu-smi 在容器里"
        f"跑不了）",
        pytrace=False,
    )


def _wait_row_gone(pid: int, *, timeout: float = 30.0) -> None:
    """等驱动把某个 PID 的 namespace 节点回收掉。

    读 ``/proc/uda/namespace_node`` 本身就会触发驱动的 idle 回收
    （``uda_ns_node_show`` → ``uda_recycle_idle_ns_node_immediately()``），所以
    这里每轮读一次就是在"催"它；回收条件是 root_tgid 已退出且该 mnt ns 里没有
    进程 —— 容器被 ``docker stop`` 停掉、进程全退之后两条都成立。

    回收不掉说明那个 ns 里还有活进程（比如我们没把容器收干净），这时表还能被
    用，必须当失败看待，不能"反正没人用"放过。
    """
    deadline = time.time() + timeout
    rows: list[dict] = []
    while time.time() < deadline:
        rows = _namespace_rows()
        if not [row for row in rows if row['root_tgid'] == pid]:
            return
        time.sleep(0.25)
    pytest.fail(
        f"{_NAMESPACE_NODE} 里 root_tgid={pid} 的节点 {timeout:.0f}s 内没有被回收："
        f"那个 mount namespace 里还有活进程，或者驱动没能回收它 —— 表还在，就还有"
        f"人能用那几张卡。当前节点：{rows}",
        pytrace=False,
    )


def _container_pid(reference: str) -> int:
    info = subprocess_run(["docker", "inspect", "-f", "{{.State.Pid}}", reference])
    return int(info.strip())


def subprocess_run(argv: list[str], *, timeout: float = 60.0) -> str:
    import subprocess

    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, (
        f"`{' '.join(argv)}` 退出码 {result.returncode}:\n"
        f"{result.stdout[:500]}\n{result.stderr[:500]}"
    )
    return result.stdout.strip()


def _start_probe_container(single_card, image: str, *, annotation: str | None,
                           extra_flags: list[str] | None = None) -> str:
    """起一个"会去初始化驱动然后挂住"的容器；返回容器名。"""
    name = f"neu-box-uda-{secrets.token_hex(6)}"
    argv = ["--name", name, "-d"]
    if annotation:
        argv += ["--annotation", f"sandbox_cgroup={annotation}"]
    # 设备节点口径与 Worker 自己起容器时一致：全部受管卡 + 必要的辅助节点。
    argv += container_device_args(single_card)
    for device in _AUX_DEVICES:
        argv += ["--device", device]
    argv += [
        "-v", f"{_ASCEND_DRIVER}:{_ASCEND_DRIVER}:ro",
        "-v", f"{_ASCEND_INFO}:{_ASCEND_INFO}:ro",
        "-v", f"{_NPU_SMI}:{_NPU_SMI}:ro",
    ]
    argv += list(extra_flags or [])
    argv += ["--entrypoint", "sh", image, "-c", _PROBE]
    single_card.created_containers.append(name)
    result = single_card.docker_run(*argv, detach=True, timeout=180)
    assert result.returncode == 0, (
        f"起探测容器失败（退出码 {result.returncode}）:\n{(result.stdout or '')[:2000]}"
    )
    return name


def _init_process_caps(pid: int) -> tuple[int, int]:
    """容器 init 进程的 (CapEff, CapBnd)，从宿主读 /proc/<pid>/status。

    和 `_exec_process_caps` 分开读：init 的能力位来自我们改写过的 bundle
    config.json（entrypoint 就是正常会初始化驱动的那个进程），exec 的来自
    docker 按 HostConfig 现算、经 `runc exec --process` 送进来的另一份 Process
    JSON。两条路各自剪、各自验，拿 exec 的结果去代表 init 会误判。
    """
    with open(f"/proc/{pid}/status", encoding="utf-8") as stream:
        text = stream.read()
    return _caps_of_status(text, source=f"/proc/{pid}/status")


def _exec_process_caps(reference: str) -> tuple[int, int]:
    """`docker exec` 进程的 (CapEff, CapBnd)。

    exec 的能力位不在 bundle 里：dockerd/containerd 按容器自己的 HostConfig
    现算一份 Process JSON 交给 `runc exec --process <file>`，跟我们改写过的
    config.json 无关（实测不剪的时候 CapEff 是全量）。所以 `neu-box-runtime`
    在 exec 子命令上单独剪 CAP_AUDIT_READ，这条用例是它的真机回归：exec 进程
    一旦是 admin，谁在这个 ns 里第一个初始化驱动，谁就决定那张 UDA 表是全量
    还是按 eBPF 放行的结果来建。
    """
    text = subprocess_run(["docker", "exec", reference, "cat", "/proc/self/status"])
    return _caps_of_status(text, source=f"docker exec {reference}")


def _caps_of_status(text: str, *, source: str) -> tuple[int, int]:
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in {"CapEff", "CapBnd"}:
            fields[key] = int(value.strip(), 16)
    assert "CapEff" in fields and "CapBnd" in fields, (
        f"{source} 里没有 CapEff/CapBnd：\n{text[:500]}"
    )
    return fields["CapEff"], fields["CapBnd"]


_RUNTIME_BIN = "/usr/local/bin/neu-box-runtime"
_CAP_AUDIT_READ_BIT = 37

_FULL_CAP_NAMES = (
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_DAC_READ_SEARCH", "CAP_FOWNER",
    "CAP_FSETID", "CAP_KILL", "CAP_SETGID", "CAP_SETUID", "CAP_SETPCAP",
    "CAP_LINUX_IMMUTABLE", "CAP_NET_BIND_SERVICE", "CAP_NET_BROADCAST",
    "CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_IPC_LOCK", "CAP_IPC_OWNER",
    "CAP_SYS_MODULE", "CAP_SYS_RAWIO", "CAP_SYS_CHROOT", "CAP_SYS_PTRACE",
    "CAP_SYS_PACCT", "CAP_SYS_ADMIN", "CAP_SYS_BOOT", "CAP_SYS_NICE",
    "CAP_SYS_RESOURCE", "CAP_SYS_TIME", "CAP_SYS_TTY_CONFIG", "CAP_MKNOD",
    "CAP_LEASE", "CAP_AUDIT_WRITE", "CAP_AUDIT_CONTROL", "CAP_SETFCAP",
    "CAP_MAC_OVERRIDE", "CAP_MAC_ADMIN", "CAP_SYSLOG", "CAP_WAKE_ALARM",
    "CAP_BLOCK_SUSPEND", "CAP_AUDIT_READ",
)


def _runtime_guard_state() -> str | None:
    """拿合成 argv/bundle 过一遍装在机器上的 wrapper，看 cap 守卫**真的**在不在。

    返回 None 表示正常，否则返回一句人话（拿去当"前置缺失"的原因）。两条路都验：

      create —— bundle/config.json 的 process.capabilities；
      exec   —— `runc exec --process <file>` 的那份 Process JSON（docker exec 的
                能力位在这里，旧版 wrapper 只管前一条，现场症状就是"init 剪了、
                exec 进去还是全套 cap"）。

    为什么要在用例之前自己跑一遍：这几条用例断言的是"容器不是 admin"，而它们失败
    的样子是"UDA 表里有 8 张卡"，看着像调度或 eBPF 的锅。前置先验一次，失败信息
    才能直接指到"装的 runtime 是旧版"。
    """
    runtime = _RUNTIME_BIN if os.access(_RUNTIME_BIN, os.X_OK) else shutil.which(
        "neu-box-runtime")
    if not runtime:
        return (f"找不到 {_RUNTIME_BIN}（装的是别的 runtime？）—— 容器不会是 "
                f"neu-box-runtime 起的，隔离无从谈起")

    with tempfile.TemporaryDirectory(prefix="neu-box-capguard-") as work:
        caps = {
            "bounding": list(_FULL_CAP_NAMES), "effective": list(_FULL_CAP_NAMES),
            "permitted": list(_FULL_CAP_NAMES), "ambient": [],
        }
        bundle = os.path.join(work, "bundle")
        os.makedirs(bundle)
        with open(os.path.join(bundle, "config.json"), "w", encoding="utf-8") as f:
            json.dump({
                "ociVersion": "1.0.2",
                "annotations": {"sandbox_cgroup": "sbx_capguard_check.slice"},
                "process": {"cwd": "/", "args": ["sh", "-c", "true"],
                            "env": ["PATH=/usr/bin"], "capabilities": caps},
            }, f)
        process = os.path.join(work, "process.json")
        with open(process, "w", encoding="utf-8") as f:
            json.dump({"cwd": "/", "args": ["sh", "-c", "true"], "env": ["PATH=/usr/bin"],
                       "capabilities": caps}, f)
        env = {**os.environ, "NEU_BOX_REAL_RUNC": "/bin/true",
               "NEU_BOX_CAP_GUARD": "drop"}

        def run(argv: list[str]) -> None:
            subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)

        run([runtime, "create", "--bundle", bundle, "--pid-file",
             os.path.join(work, "pid"), "capguard-check"])
        run([runtime, "exec", "--process", process, "capguard-check"])

        with open(os.path.join(bundle, "config.json"), encoding="utf-8") as f:
            create_text = f.read()
        with open(process, encoding="utf-8") as f:
            exec_spec = json.load(f)

    if "CAP_AUDIT_READ" in create_text:
        return ("装的 neu-box-runtime 没剪 create 的能力位（config.json 里 "
                "CAP_AUDIT_READ 还在）：--privileged / --cap-add=ALL 的容器会被 "
                "Ascend 驱动判成 admin，UDA 表是全量的。装新版 RPM 再跑")
    if "CAP_AUDIT_READ" in json.dumps(exec_spec):
        return ("装的 neu-box-runtime 没剪 docker exec 的能力位（exec 的 Process "
                "JSON 里 CAP_AUDIT_READ 还在）：init 剪过，exec 进去的进程仍是 "
                "admin。装新版 RPM（0.1.0-2 起管 exec）再跑")
    if not exec_spec.get("cwd"):
        return ("wrapper 改 exec 的 Process JSON 时把 cwd/args 丢了 —— 那条路径的 "
                "断言不可信，先修 wrapper")
    return None


@pytest.fixture(scope="session")
def driver_isolation(deployment, container, single_card):
    """前置：root（读 /proc/uda）、docker、宿主上装了 npu-smi 与驱动目录、≥1 张空闲卡。"""
    if os.geteuid() != 0:
        pytest.fail(
            "前置缺失：这一组要读 /proc/uda/namespace_node，必须以 root 运行",
            pytrace=False,
        )
    for path in (_NAMESPACE_NODE, _NPU_SMI, _ASCEND_DRIVER, _ASCEND_INFO):
        if not os.path.exists(path):
            pytest.fail(
                f"前置缺失：{path} 不存在（这台机器没装 Ascend 驱动/npu-smi？）",
                pytrace=False,
            )
    guard_problem = _runtime_guard_state()
    if guard_problem:
        pytest.fail(f"前置缺失：{guard_problem}", pytrace=False)
    return deployment


def test_registered_container_sees_only_its_sandbox_cards(
        driver_isolation, single_card, container_image):
    """61 · 已登记容器：UDA 表里只有沙盒持有的那些卡（cap 守卫的真机回归）。"""
    first, second = single_card.require_idle(2)
    terminal = single_card.spawn_terminal()

    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[first, second]),
    ) as sandbox:
        name = sandbox["sandbox_name"]
        reference = _start_probe_container(
            single_card, container_image, annotation=name)
        row = _wait_row(_container_pid(reference))
        assert row['dev_num'] == 2, (
            f"沙盒 {name} 持有 {first}/{second} 两张卡，但驱动给这个容器的 UDA 表里"
            f"有 {row['dev_num']} 张（udevid={row['udevids']}）。超出的部分说明容器被"
            f"判成了 admin（cap 守卫没生效，或容器是 --cap-add=ALL 且没被剪位），"
            f"整行：{row}"
        )
        assert sorted(row['udevids']) == sorted([first, second]), row


def test_unregistered_container_sees_no_cards(
        driver_isolation, single_card, container_image):
    """62 · 没经过 neu-box 的容器：一张卡都看不到（fail-closed）。"""
    reference = _start_probe_container(
        single_card, container_image, annotation=None)
    row = _wait_row(_container_pid(reference))
    assert row['dev_num'] == 0, (
        f"没有 sandbox_cgroup annotation 的容器竟然拿到 {row['dev_num']} 张卡"
        f"（udevid={row['udevids']}）—— 未登记的容器必须一张都拿不到，整行：{row}"
    )


def test_host_shell_without_reservation_sees_shared_cards(
        driver_isolation, single_card):
    """63 · 宿主未申请的 shell：共享卡可见、被独占的看不见。"""
    total = single_card.total_devices()
    reserved: set[int] = set()
    for sandbox in single_card.sandboxes():
        for device in sandbox.get("devices") or []:
            reserved.add(int(str(device).split(":")[-1]))
    for task in single_card.queue():
        if task.get("status") == "running":
            for device in task.get("devices") or []:
                reserved.add(int(str(device).split(":")[-1]))

    # 自己也占一张。**没有"被独占的卡"，这条用例就没有牙**：进程能把全部受管卡
    # 都打开时，驱动不给它单独建 ns 节点，它复用 root_tgid=1 那张宿主共享表 ——
    # 判据（dev_num == 总数 − 被独占数）就落到一张别人建的表上，实测表现是
    # "前置缺失"（其实是探针拿到了宿主节点，不是没建节点）。
    device = single_card.require_idle(1)[0]
    blocker = single_card.submit("sleep 30", device_ids=[device])
    single_card.wait_task_running(blocker)
    reserved.add(device)

    process = _start_host_probe()
    if process is None:
        pytest.fail(
            "前置缺失：本机没有能触发驱动建 UDA 节点的宿主命令。`npu-smi info` "
            "不算 —— 它走 DCMI/manager，不建 /proc/uda 的 ns 节点；真正会建的是"
            "拿设备做计算的进程。用 NEU_BOX_DEVICE_PROBE_CMD 指定一条，例如：\n"
            "  NEU_BOX_DEVICE_PROBE_CMD='/home/yuxd/miniconda3/bin/python -c "
            "\"import torch, torch_npu\"'\n"
            f"已经试过的候选与原因：{_PROBE_FAILURES or '（没有候选，检查 /dev 与 python）'}",
            pytrace=False,
        )
    try:
        row = _wait_row(process.pid)
    finally:
        process.kill()
        process.wait(timeout=10)
        single_card.client.delete_tasks([blocker])
        single_card.wait_task(blocker)
    expected = total - len(reserved)
    assert row['dev_num'] == expected, (
        f"宿主没有申请卡的 shell 应当看到 {expected} 张（{total} 张减去被独占的 "
        f"{sorted(reserved)}），实际 {row['dev_num']} 张（udevid={row['udevids']}）"
    )


def test_full_capabilities_container_is_not_admin(
        driver_isolation, single_card, container_image):
    """64 · `--cap-add=ALL` 的容器：被剪掉一位能力（init 与 docker exec 都剪），
    且仍然只看到沙盒的卡。"""
    device = single_card.require_idle(1)[0]
    terminal = single_card.spawn_terminal()

    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    ) as sandbox:
        name = sandbox["sandbox_name"]
        reference = _start_probe_container(
            single_card, container_image, annotation=name,
            extra_flags=["--cap-add=ALL"],
        )
        caps, bounding = _init_process_caps(_container_pid(reference))
        assert (
            caps & (1 << _CAP_AUDIT_READ_BIT) == 0
            and bounding & (1 << _CAP_AUDIT_READ_BIT) == 0
        ), (
            f"容器 init 仍然握着 CAP_AUDIT_READ(bit 37)：CapEff={caps:#x} "
            f"CapBnd={bounding:#x}。cap 守卫没生效（装的 neu-box-runtime 是旧版？"
            f"用 bash /tmp/cgcheck/check.sh 一验就知道）"
        )
        assert caps & (1 << 21) != 0, (
            f"CapEff={caps:#x} 里连 CAP_SYS_ADMIN 都没有 —— 这次 --cap-add=ALL 没传进去，"
            f"用例前提不成立（先确认 docker 收到的参数）"
        )

        # exec 走的是另一份 Process JSON（docker 按 HostConfig 现算），也得剪位。
        # 现场症状就是这个：init 剪过了，`docker exec` 进去还是全套 cap。
        exec_caps, exec_bounding = _exec_process_caps(reference)
        assert (
            exec_caps & (1 << _CAP_AUDIT_READ_BIT) == 0
            and exec_bounding & (1 << _CAP_AUDIT_READ_BIT) == 0
        ), (
            f"`docker exec` 出来的进程仍然握着 CAP_AUDIT_READ(bit 37)："
            f"CapEff={exec_caps:#x} CapBnd={exec_bounding:#x}。exec 的能力位由 docker "
            f"按容器 HostConfig 现算、经 `runc exec --process` 送进来，必须由 runtime "
            f"在 exec 子命令上单独剪 —— 装的 neu-box-runtime 是旧版？"
        )

        row = _wait_row(_container_pid(reference))
        assert row['dev_num'] == 1, (
            f"沙盒 {name} 只持有卡 {device}，但这个 --cap-add=ALL 容器的 UDA 表里有 "
           f"{row['dev_num']} 张（udevid={row['udevids']}）—— 剪掉 CAP_AUDIT_READ "
           f"没有把它踢出 admin 分支，整行：{row}"
        )


def test_exec_process_borrows_the_same_authorization(
        driver_isolation, single_card, container_image):
    """71 · `docker exec` 起的进程：借的还是这个沙盒那一份授权，多一张都不给。

    容器里只有 exec 这一条路不重跑 OCI hook —— 它复用的是 create 时登记好的
    mnt ns，而授权是**按 mnt ns 委托**的（见 native/sandbox/bpf/device_block.bpf.c
    的容器分支），所以"新起的进程还能开几张卡"要单独验：自己沙盒的卡照开，
    没人预留的卡一律拒绝（容器分支不看"设备空不空"，只看委托方有没有预留）。

    顺便验 exec 的能力位也被剪过（64 只测了它自己那一份 CapEff）—— 一个还是
    admin 的 exec 进程会绕开这整套判定。
    """
    first, second = single_card.require_idle(2)
    terminal = single_card.spawn_terminal()

    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[first]),
    ) as sandbox:
        name = sandbox["sandbox_name"]
        reference = _start_probe_container(
            single_card, container_image, annotation=name)
        _wait_row(_container_pid(reference))

        # 探测放在子 shell 里、只看退出码：非交互 sh 遇到重定向失败会直接退出
        # 整个脚本（和 container_probe_command 同一套判据）。
        probes = "; ".join(
            f"out=$( ( exec 3<{single_card.device_node(minor)} ) 2>&1 ); rc=$?; "
            f"echo SBX_EXEC_{minor}=$rc"
            for minor in (first, second)
        )
        output = subprocess_run(["docker", "exec", reference, "sh", "-c", probes])
        results = {
            int(minor): int(rc)
            for minor, rc in re.findall(r"SBX_EXEC_(\d+)=(\d+)", output)
        }
        assert results.get(first) == 0, (
            f"`docker exec` 出来的进程打不开自己沙盒的卡 {first}（探测结果 "
            f"{results}，原始输出：{output[:500]}）—— exec 复用的是同一个 mnt ns，"
            f"授权不该少"
        )
        assert results.get(second, 0) != 0, (
            f"`docker exec` 出来的进程打开了没人预留的卡 {second}（探测结果 "
            f"{results}）—— 容器只能开委托方预留的那些卡，空闲卡也不行"
        )

        exec_caps, exec_bounding = _exec_process_caps(reference)
        assert (
            exec_caps & (1 << _CAP_AUDIT_READ_BIT) == 0
            and exec_bounding & (1 << _CAP_AUDIT_READ_BIT) == 0
        ), (
            f"`docker exec` 的进程还握着 CAP_AUDIT_READ(bit 37)：CapEff="
            f"{exec_caps:#x} CapBnd={exec_bounding:#x} —— 它会被驱动判成 admin，"
            f"上面两条 open 断言的结论也就靠不住了"
        )


def test_release_reaps_container_and_driver_table(
        driver_isolation, single_card, container_image):
    """72 · release 之后：容器停着（不删）、驱动那张按 mnt ns 缓存的表回收、卡干净地交给下一家。

    驱动的 UDA 表是隔离的第二道门，它按 mnt ns 缓存、**不复核**我们的委托表：
    撤掉 BPF 授权只挡住"新的 open"，已经建好的表照样让那个 ns 里的进程用卡
    （torch 走 manager + UDA，不需要 open davinciN）。所以释放沙盒必须把容器
    真的**停**掉（不删：可写层留给用户）—— 死 ns 里没有进程，表也就没人能用；
    再催一次驱动的 idle 回收，让那张表从账上消失，不给下一个用户留任何
    "上一家的名字还挂在驱动里"的状态。
    """
    baseline = single_card.idle_devices()
    device = single_card.require_idle(1)[0]
    terminal = single_card.spawn_terminal()

    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    ) as sandbox:
        name = sandbox["sandbox_name"]
        reference = _start_probe_container(
            single_card, container_image, annotation=name)
        init_pid = _container_pid(reference)
        row = _wait_row(init_pid)
        assert row['dev_num'] == 1 and row['udevids'] == [device], row

        released = single_card.client.release(
            name, timeout=single_card.task_timeout,
        )
        assert released.status == 200, released.text

        # ① 容器被停掉（**不删**：可写层留给用户），进程没了 = 授权撤干净了。
        #    "还跑着的容器"才是真泄露：它带着已撤销的授权继续用卡，而卡已经
        #    放回空闲池（42 只看了"登记记录没了"，这里连着看容器和驱动那张表）。
        single_card.wait_container_stopped(reference)
        # ② 驱动那张表被回收（读 /proc/uda/namespace_node 会催它）。
        _wait_row_gone(init_pid)
        single_card.wait_idle_at_least(baseline)

    # ③ 同一张卡交给下一个沙盒：新容器看到的仍然只有它自己那一张 —— 上一家
    #    用过的表、以及它在驱动里留下的设备占用，都不能挡路或者串给下一家。
    second_terminal = single_card.spawn_terminal()
    with single_card.sandbox(
        single_card.acquire_payload(second_terminal.pid, device_ids=[device]),
    ) as sandbox:
        other = _start_probe_container(
            single_card, container_image, annotation=sandbox["sandbox_name"])
        row = _wait_row(_container_pid(other))
        assert row['dev_num'] == 1 and row['udevids'] == [device], (
            f"卡 {device} 交给新沙盒 {sandbox['sandbox_name']} 后，容器的 UDA 表是 "
            f"{row} —— 期望只有这一张（drv 里按 mnt ns 缓存的表/占用没有清干净？）"
        )
