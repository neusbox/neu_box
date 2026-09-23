"""第 3 层 · client(neubox) 的容器路径（manifest 67-70、73-75）。

`test_client.py` 验的是 CLI 的基本路径（acquire / list / submit / cancel），
不需要 dockerd。这一组把 `neubox docker run` 起的容器**从生到死**走完整：注入
annotation → runtime hook 登记 → 容器里能不能用卡 → stop / start / release /
exec 各条路径。最典型的那条是：

    neubox acquire --device-num 2
    neubox docker run ... --name neu-test      # 登记到沙盒
    docker stop neu-test                       # 登记被注销，沙盒仍占着卡
    neubox release                             # 沙盒销毁；容器留着（停着）
    docker start neu-test                      # 重走 hook → 旧 annotation 404 → 起不来
    docker exec neu-test ...                   # 没 running，docker 直接拒

要点是：**释放之后拿不回卡，但不是靠删容器实现的**。删容器会连可写层一起丢，
用户可能还要 commit / cp 出来，所以 Worker 只停不删；隔离靠"进程退光 → mnt ns
死掉 → 驱动那张按 mnt ns 缓存的 UDA 表没人能用"+"重新 start 必然重走 hook 且
登记被拒"。原理见 `docs/isolation.md`。

前置：`neubox` 没装整组跳过（和 `test_client.py` 同一套软缺失语义 —— 见
`conftest.py` 的 ``neubox_bin`` fixture），装了但版本不够直接失败。
"""

from __future__ import annotations

import secrets
import shlex
import subprocess

from deployment_support import (
    CONTAINER_OPEN_OK,
    CONTAINER_PROBE_MARKER,
    container_probe_command,
    neubox_cli,
    neubox_sandbox_name,
    wait_container_log_count,
)


def _docker_run_args(name: str, node: str, image: str, command: str) -> list[str]:
    """`neubox docker run` 的参数：容器名 + 只挂一个设备节点 + 探测命令。"""
    return [
        "-d", "--name", name, "--device", node,
        "--entrypoint", "sh", image, "-c", command,
    ]


def _docker_run_in_sandbox(single_card, neubox_bin, device, args: list[str], *,
                           container_name: str) -> tuple[int, str, str]:
    """借一个 shell 进沙盒，在里面跑 `neubox docker run`；返回 (rc, 输出, 沙盒名)。

    `neubox docker run` 是按自己 PID 的 cgroup 反查沙盒的，所以必须在沙盒里跑；
    沙盒本身也用**真 neubox** 借（`neubox acquire --pid <shell>`），这一组不留
    任何绕过 CLI 的旁路。
    """
    shell = single_card.sandbox_shell()
    sandbox = neubox_sandbox_name(
        neubox_bin, "acquire", "--pid", str(shell.pid), "--device", str(device),
    )
    # CLI 建的沙盒不走 acquire_sandbox，得自己登记进收尾清单（失败路径上要靠它
    # 把卡放回去）。
    single_card.track_sandbox(sandbox)
    single_card.created_containers.append(container_name)
    # 每个参数都要 quote：`-c "sleep 600"` 里的引号属于 docker 的 argv，不能
    # 在拼 shell 命令时被吃掉（否则容器跑的是 `sh -c sleep`，立刻退出）。
    command = " ".join(
        shlex.quote(str(item))
        for item in [neubox_bin, "docker", "run", *args]
    )
    rc, output = shell.run(command)
    return rc, output, sandbox


def _assert_open(single_card, reference: str, node: str, *, expect_ok: bool,
                 context: str) -> None:
    """在容器里 open 一个设备节点，只看退出码（镜像里不一定有 libc 消息目录）。"""
    probe = single_card.docker(
        "exec", reference, "sh", "-c",
        f"(exec 3<{node}) 2>/dev/null && echo OK || echo DENIED",
        timeout=60,
    )
    verdict = (probe.stdout or "").strip()
    if expect_ok:
        assert verdict == "OK", (
            f"{context}，但容器里打不开 {node}：verdict={verdict!r} "
            f"rc={probe.returncode} 输出={(probe.stdout or '')[:300]}"
        )
    else:
        assert "OK" not in verdict, (
            f"{context}，容器里却打开了 {node}：verdict={verdict!r} "
            f"输出={(probe.stdout or '')[:300]}"
        )


def _assert_start_refused(single_card, reference: str, sandbox: str) -> None:
    """容器必须起不来：start 会重走 hook，annotation 指向的沙盒已经没了 → 404。"""
    started = single_card.docker("start", reference, timeout=90)
    if started.returncode == 0:
        # 真起来了就别让它继续跑（会污染后面的用例），但这条断言必须失败。
        single_card.docker("stop", "-t", "2", reference, timeout=60)
    assert started.returncode != 0, (
        f"沙盒 {sandbox} 已经销毁，容器 {reference} 却还能 `docker start` 起来 —— "
        f"start 会重走 OCI hook、带了旧 annotation，Worker 应当 404 拒绝、"
        f"`runc create` 必须失败。输出：{(started.stdout or '')[:500]}"
    )
    single_card.wait_container_stopped(reference)


def _stage_stopped_released_container(single_card, neubox_bin, container_image,
                                      device: int, node: str):
    """搭出"容器停着、沙盒已 release"的现场；返回 ``(沙盒名, 容器名, 容器 id)``。

    这是那条经典路径的中段，73/74/75 三条用例共用，每一步都断言 —— 否则后面
    的失败信息会指到错误的环节上。
    """
    baseline = single_card.idle_devices()
    name = f"neu-box-client-{secrets.token_hex(4)}"
    rc, output, sandbox = _docker_run_in_sandbox(
        single_card, neubox_bin, device,
        _docker_run_args(name, node, container_image,
                         container_probe_command(node)),
        container_name=name,
    )
    assert rc == 0, f"`neubox docker run` 失败（rc={rc}）：\n{output[:2000]}"

    container_id = single_card.container_id_of(name)
    assert single_card.wait_container_registered(container_id) == sandbox, (
        f"容器没有登记到它所在的沙盒 {sandbox}"
    )
    text = wait_container_log_count(
        single_card, name, CONTAINER_PROBE_MARKER, 1,
    )
    assert CONTAINER_OPEN_OK in text, (
        f"容器里打不开自己沙盒的卡，后面的路径没有意义：\n{text[:1000]}"
    )

    # ① stop：登记被注销（pidfd），但沙盒自己还占着这张卡。
    stopped = single_card.docker("stop", "-t", "2", name, timeout=90)
    assert stopped.returncode == 0, (stopped.stdout or "")[:500]
    single_card.wait_container_unregistered(container_id)
    assert single_card.idle_devices() == baseline - 1, (
        f"容器停了之后卡就回池了（{single_card.idle_devices()} != {baseline - 1}），"
        f"但沙盒 {sandbox} 还持有它"
    )

    # ② release：沙盒销毁、卡回池；容器**留着**（停着）。
    neubox_cli(neubox_bin, "release", sandbox)
    single_card.wait_sandbox_gone(sandbox)
    single_card.wait_idle_at_least(baseline)
    single_card.wait_container_stopped(name)
    return sandbox, name, container_id


def test_client_docker_run_registers_container_in_own_sandbox(
        neubox_bin, single_card, container_image):
    """67 · 真 neubox：`docker run` 注入 annotation → 容器登记到自己那个沙盒。

    这是 client 侧最关键的一条：annotation 拼错、argv 位置错（历史上少传
    argv[0] 就会整条命令失败）、或者按 PID 反查到别的沙盒，都会在这里露出来。
    """
    device = single_card.require_idle(1)[0]
    name = f"neu-box-client-{secrets.token_hex(4)}"
    node = single_card.device_node(device)

    rc, output, sandbox = _docker_run_in_sandbox(
        single_card, neubox_bin, device,
        _docker_run_args(name, node, container_image, "sleep 600"),
        container_name=name,
    )
    assert rc == 0, f"沙盒 {sandbox} 里的 `neubox docker run` 失败（rc={rc}）：\n{output[:2000]}"

    container_id = single_card.container_id_of(name)
    registered = single_card.wait_container_registered(container_id)
    assert registered == sandbox, (
        f"容器登记到了 {registered}，期望它所在的沙盒 {sandbox}"
    )

    # annotation 的值必须就是沙盒名（不能是 cgroup 路径/basename）。
    annotations = single_card.docker(
        "inspect", "-f", '{{index .HostConfig.Annotations "sandbox_cgroup"}}',
        name, timeout=60,
    )
    assert annotations.returncode == 0, annotations.stderr[:500]
    assert annotations.stdout.strip() == sandbox, (
        f"容器上的 annotation 是 {annotations.stdout.strip()!r}，期望沙盒名 {sandbox!r}"
    )

    single_card.remove_container(name)


def test_client_docker_run_refuses_without_sandbox(neubox_bin, container_image):
    """68 · 不在沙盒里的 `neubox docker run`：拒绝启动，不降级成"没 annotation 的裸跑"。"""
    result = subprocess.run(
        [neubox_bin, "docker", "run", "--rm", "--entrypoint", "true",
         container_image],
        capture_output=True, text=True, timeout=120,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        f"不在沙盒里竟然也起来了（rc=0）：\n{output[:1000]}"
    )
    assert "acquire" in output or "沙盒" in output, (
        f"报错没有说清要先 acquire：\n{output[:1000]}"
    )


def test_client_docker_run_container_sees_only_reserved_card(
        neubox_bin, single_card, container_image):
    """69 · client 起的容器里：自己沙盒的卡能开，别人的/空闲的卡开不了。"""
    device = single_card.require_idle(1)[0]
    other = [minor for minor in single_card.idle_minors() if minor != device]
    name = f"neu-box-client-{secrets.token_hex(4)}"

    rc, output, sandbox = _docker_run_in_sandbox(
        single_card, neubox_bin, device,
        _docker_run_args(name, single_card.device_node(device), container_image,
                         "sleep 600"),
        container_name=name,
    )
    assert rc == 0, f"`neubox docker run` 失败（rc={rc}）：\n{output[:2000]}"

    single_card.wait_container_registered(single_card.container_id_of(name))
    _assert_open(single_card, name, single_card.device_node(device),
                 expect_ok=True,
                 context=f"沙盒 {sandbox} 持有卡 {device}")
    if other:
        _assert_open(single_card, name, single_card.device_node(other[0]),
                     expect_ok=False,
                     context=f"沙盒 {sandbox} 只持有 {device}，卡 {other[0]} 是空闲的")

    single_card.remove_container(name)


def test_client_release_stops_client_started_container(
        neubox_bin, single_card, container_image):
    """70 · `neubox release` 把 client 起的容器一并停掉（授权撤销、容器保留）。"""
    baseline = single_card.idle_devices()
    device = single_card.require_idle(1)[0]
    name = f"neu-box-client-{secrets.token_hex(4)}"

    rc, output, sandbox = _docker_run_in_sandbox(
        single_card, neubox_bin, device,
        _docker_run_args(name, single_card.device_node(device), container_image,
                         "sleep 600"),
        container_name=name,
    )
    assert rc == 0, f"`neubox docker run` 失败（rc={rc}）：\n{output[:2000]}"
    container_id = single_card.container_id_of(name)
    assert single_card.wait_container_registered(container_id) == sandbox

    neubox_cli(neubox_bin, "release", sandbox)
    single_card.wait_sandbox_gone(sandbox)
    single_card.wait_container_stopped(name)
    single_card.wait_idle_at_least(baseline)


def test_client_container_stop_then_start_uses_card_again(
        neubox_bin, single_card, container_image):
    """73 · 沙盒还在时 `docker stop` → `docker start`：容器能回来、重新登记、还能用卡。

    这是"支持 stop / start"的正例：stop 让 Worker 注销登记（pidfd 事件），但沙盒
    自己还占着卡；start 重走整条 create → runtime hook → 重新登记到同一个沙盒
    （新的 mnt ns），容器里的 NPU 照旧能用。
    """
    baseline = single_card.idle_devices()
    device = single_card.require_idle(1)[0]
    node = single_card.device_node(device)
    name = f"neu-box-client-{secrets.token_hex(4)}"

    rc, output, sandbox = _docker_run_in_sandbox(
        single_card, neubox_bin, device,
        _docker_run_args(name, node, container_image,
                         container_probe_command(node)),
        container_name=name,
    )
    assert rc == 0, f"`neubox docker run` 失败（rc={rc}）：\n{output[:2000]}"
    container_id = single_card.container_id_of(name)
    assert single_card.wait_container_registered(container_id) == sandbox
    first = wait_container_log_count(single_card, name, CONTAINER_PROBE_MARKER, 1)
    assert CONTAINER_OPEN_OK in first, first[:1000]

    stopped = single_card.docker("stop", "-t", "2", name, timeout=90)
    assert stopped.returncode == 0, (stopped.stdout or "")[:500]
    single_card.wait_container_unregistered(container_id)
    assert single_card.idle_devices() == baseline - 1, (
        "注销登记不该把沙盒占着的卡放回空闲池"
    )

    started = single_card.docker("start", name, timeout=90)
    assert started.returncode == 0, (
        f"沙盒 {sandbox} 还活着，`docker start` 却失败了："
        f"{(started.stdout or '')[:500]}"
    )
    assert single_card.wait_container_registered(container_id) == sandbox, (
        f"容器重启后没有重新登记到沙盒 {sandbox}"
    )
    text = wait_container_log_count(single_card, name, CONTAINER_PROBE_MARKER, 2)
    assert text.count(CONTAINER_OPEN_OK) >= 2, (
        f"重启后容器里打不开自己沙盒的卡（登记在、授权没恢复）：\n{text[:1500]}"
    )

    neubox_cli(neubox_bin, "release", sandbox)
    single_card.wait_sandbox_gone(sandbox)
    single_card.wait_idle_at_least(baseline)


def test_client_release_after_stop_keeps_container_but_kills_access(
        neubox_bin, single_card, container_image):
    """74 · stop 之后再 release：容器留着（可写层不丢），但再也拿不回卡。

    这条路上 release 在 ``containers`` 表里已经看不到容器了（stop 时就注销了），
    只剩按 label 兜底的扫描 —— 而 `neubox docker run` 起的容器没有 label，所以
    它会**活过 release**（停着）。隔离的另一半必须接住：start 重走 hook 时
    annotation 指向已销毁的沙盒 → 404 → 容器起不来；exec 也没得执行。
    """
    baseline = single_card.idle_devices()
    device = single_card.require_idle(1)[0]
    node = single_card.device_node(device)

    sandbox, name, _container_id = _stage_stopped_released_container(
        single_card, neubox_bin, container_image, device, node)

    # 容器还在（只是停着）—— 这是"不删容器"的直接回归：可写层没丢。
    _assert_start_refused(single_card, name, sandbox)
    _assert_open(single_card, name, node, expect_ok=False,
                 context=f"沙盒 {sandbox} 已释放、容器也没起来")
    single_card.wait_idle_at_least(baseline)

    single_card.remove_container(name)


def test_client_released_card_goes_to_next_sandbox(
        neubox_bin, single_card, container_image):
    """75 · 停着的老容器不会挡住同一张卡交给下一个沙盒。

    接 74 的现场：老容器还在（停着，annotation 指向已销毁的沙盒）。同一张卡用
    真 neubox 重新 acquire，新容器登记成功、容器里能开这张卡；老容器这时 start
    仍然起不来 —— 授权不在它手上。
    """
    baseline = single_card.idle_devices()
    device = single_card.require_idle(1)[0]
    node = single_card.device_node(device)

    sandbox, old_name, _old_id = _stage_stopped_released_container(
        single_card, neubox_bin, container_image, device, node)
    single_card.wait_container_stopped(old_name)

    new_name = f"neu-box-client-{secrets.token_hex(4)}"
    rc, output, new_sandbox = _docker_run_in_sandbox(
        single_card, neubox_bin, device,
        _docker_run_args(new_name, node, container_image, "sleep 600"),
        container_name=new_name,
    )
    assert rc == 0, f"同一张卡重新 acquire 后 `neubox docker run` 失败：\n{output[:2000]}"
    assert new_sandbox != sandbox, (
        f"新沙盒名和释放掉的那个一样（{new_sandbox}）—— 沙盒名不该复用"
    )
    new_id = single_card.container_id_of(new_name)
    assert single_card.wait_container_registered(new_id) == new_sandbox
    _assert_open(single_card, new_name, node, expect_ok=True,
                 context=f"卡 {device} 已经交给新沙盒 {new_sandbox}")

    # 老容器还停着，start 依然被拒。
    _assert_start_refused(single_card, old_name, sandbox)

    neubox_cli(neubox_bin, "release", new_sandbox)
    single_card.wait_sandbox_gone(new_sandbox)
    single_card.wait_idle_at_least(baseline)
    single_card.remove_container(new_name)
    single_card.remove_container(old_name)
