"""第 3 层 · 容器（manifest 32-39）。

组夹具 ``container`` 已经确认 dockerd 可用、且 ``default-runtime`` 真的是
``neu-box-runtime`` —— 容器能不能拿到设备全靠这条 hook 链，指错了后面全是
假阳性。

容器**不住在沙盒 cgroup 里**：它通过 OCI runtime hook 把 init 的 mount
namespace 登记到 Worker，借用沙盒的授权。所以这里的观察点有两个：
``GET /sandbox/status?container=<id>``（登记有没有落库）和容器内能不能打开
设备节点（BPF 授权有没有生效）。

需要设备的那几条用例额外要 ``single_card`` —— manifest 给这些行写的前置是
"1 卡 + dockerd + runtime"，而 32/35/36/37 只要 dockerd 和 runtime，卡不是
它们的前置，所以没把卡数塞进 ``container`` 夹具。
"""

from __future__ import annotations

import secrets
import shutil

import pytest

from deployment_support import (
    CONTAINER_OPEN_OK,
    CONTAINER_PROBE_MARKER,
    container_probe_command,
    require_container_not_running,
    run_container,
    wait_container_log_count,
)

RUNTIME_BINARY = "/usr/local/bin/neu-box-runtime"
HOOK_BINARY = "/usr/local/bin/neu-box-hook"


def _minor(device: str) -> int:
    return int(str(device).split(":")[-1])


def test_default_runtime_points_at_neu_box_runtime(container):
    """32 · default-runtime 真的对。"""
    runtime = container.default_runtime()
    assert runtime == "neu-box-runtime", (
        f"dockerd 的 default-runtime 是 {runtime or '(空)'!r}，应为 "
        f"'neu-box-runtime'；不是它的话 OCI runtime wrapper 不会被调用，"
        f"容器登记无从发生（配置见 /etc/docker/daemon.json，改完必须 restart docker）"
    )

    info = container.docker("info", timeout=60).stdout or ""
    assert "neu-box-runtime" in info, (
        "docker info 的 runtimes 表里没有 neu-box-runtime:\n" + info[:2000]
    )

    if shutil.which(RUNTIME_BINARY) is None:
        pytest.fail(
            f"default-runtime 指向 neu-box-runtime，但 {RUNTIME_BINARY} "
            f"不存在或不可执行；dockerd 会在下一次起容器时直接失败",
            pytrace=False,
        )
    if shutil.which(HOOK_BINARY) is None:
        pytest.fail(
            f"缺少 OCI hook 二进制 {HOOK_BINARY}；wrapper 会把这条不存在的"
            f"路径写进 config.json，容器会在 runc create 阶段失败",
            pytrace=False,
        )


def test_annotated_container_is_registered_and_reaches_device(
        container, single_card, container_image):
    """33 · 带 annotation → 被登记 → 容器内读到卡。

    "容器内初始化 NPU" 的完整形态要镜像里带驱动工具链，部署机上不保证有；
    这里取的是同一件事的可观察内核：容器里的进程能把设备节点打开。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    node = single_card.device_node(device)
    terminal = single_card.spawn_terminal()
    sandbox = single_card.acquire_sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    name = sandbox["sandbox_name"]

    reference, result = run_container(
        single_card, container_image, annotation=name,
        command=container_probe_command(node),
    )
    assert result.returncode == 0, (
        f"带 annotation 的容器起不来（docker run 退出码 {result.returncode}）:\n"
        f"{(result.stdout or '')[:2000]}\n"
        f"容器起不来通常就是 hook 登记被拒 —— 看 Worker 日志里 "
        f"/container/register 的结果"
    )

    container_id = single_card.container_id_of(reference)
    registered = single_card.wait_container_registered(container_id)
    assert registered == name, f"容器登记到了沙盒 {registered}，期望 {name}"

    text = wait_container_log_count(
        single_card, reference, CONTAINER_PROBE_MARKER, 1,
    )
    assert CONTAINER_OPEN_OK in text, (
        f"容器内的设备节点 {node} 打不开 —— 登记落了库、BPF 授权却没生效：\n"
        f"{text[:2000]}"
    )

    single_card.remove_container(reference)
    single_card.release_sandbox(name)
    single_card.wait_idle_at_least(baseline)


def test_container_without_annotation_gets_no_device(
        container, single_card, container_image):
    """34 · 不带 annotation → 拿不到卡。"""
    device = single_card.idle_minors()[0]
    node = single_card.device_node(device)

    reference, result = run_container(
        single_card, container_image, command=container_probe_command(node),
    )
    assert result.returncode == 0, (
        f"不带 annotation 的容器应该照常启动（wrapper 对没有 annotation 的 "
        f"bundle 一个字节都不改），实际退出码 {result.returncode}:\n"
        f"{(result.stdout or '')[:2000]}\n"
        f"唯一的例外是 default-runtime 没配成 neu-box-runtime，"
        f"那种情况第 32 条已经失败过了"
    )

    text = wait_container_log_count(
        single_card, reference, CONTAINER_PROBE_MARKER, 1,
    )
    assert CONTAINER_OPEN_OK not in text, (
        f"没有登记的容器竟然打开了设备节点 {node} —— 容器授权是 fail-closed，"
        f"这一条应当必然失败:\n{text[:2000]}"
    )

    container_id = single_card.container_id_of(reference)
    status = single_card.sandbox_of_container(container_id)
    assert status.status == 200, status.text
    assert status.json().get("sandbox_name") is None, (
        f"没有 annotation 的容器居然有登记: {status.text[:500]}"
    )
    single_card.remove_container(reference)


def test_annotation_pointing_at_unknown_sandbox_blocks_start(
        container, container_image):
    """35 · annotation 指向不存在的沙盒 → 容器起不来。"""
    missing = f"sbx_{container.user}_{secrets.token_hex(6)}.slice"
    reference, result = run_container(
        container, container_image, annotation=missing, command="sleep 60",
        detach=False,
    )
    assert result.returncode != 0, (
        f"annotation 指向不存在的沙盒 {missing} 时容器竟然起来了；"
        f"hook 必须退非 0、runc create 必须失败（绝不降级放行）:\n"
        f"{(result.stdout or '')[:2000]}"
    )
    require_container_not_running(
        container, reference,
        context=f"hook 拒绝了不存在的沙盒 {missing}",
    )
    container.remove_container(reference)


@pytest.mark.deployment_restart
def test_worker_down_blocks_annotated_container(container, container_image):
    """36 · Worker 停掉 → 容器起不来。

    停服用 ``stop_worker()``（SIGTERM MainPID，和 ``pause`` 停服做的事一样；
    ``systemctl stop`` 被单元的 ``RefuseManualStop=yes`` 拒绝），恢复用
    ``start_worker()``（``neuboxctl setup``）。这里不能用
    ``neuboxctl pause`` 停：它要先等到 ``quiet=true``，而本用例手里
    还攥着一个 active 沙盒，pause 只会一直等。
    """
    container.require_service_control()
    terminal = container.spawn_terminal()
    sandbox = container.acquire_sandbox(
        container.acquire_payload(terminal.pid, device_num=0),
    )
    name = sandbox["sandbox_name"]

    container.stop_worker()
    try:
        reference, down = run_container(
            container, container_image, annotation=name, command="sleep 60",
            detach=False,
        )
        assert down.returncode != 0, (
            f"Worker 已经停了，带 annotation 的容器居然还能起来 —— hook 连不上 "
            f"Worker 时必须退非 0:\n{(down.stdout or '')[:2000]}"
        )
        require_container_not_running(
            container, reference, context="Worker 停着的时候 hook 连不上 Worker",
        )
        container.remove_container(reference)
    finally:
        container.start_worker()

    reference, up = run_container(
        container, container_image, annotation=name, command="sleep 60",
    )
    assert up.returncode == 0, (
        f"Worker 恢复后同一个容器仍然起不来（退出码 {up.returncode}）:\n"
        f"{(up.stdout or '')[:2000]}"
    )
    container_id = container.container_id_of(reference)
    assert container.wait_container_registered(container_id) == name

    container.remove_container(reference)
    container.release_sandbox(name)


def test_unrelated_container_unaffected(container, container_image):
    """37 · 无关容器不受影响。"""
    marker = f"plain-container-{secrets.token_hex(4)}"
    reference, result = run_container(
        container, container_image, command=f"echo {marker}", detach=False,
    )
    assert result.returncode == 0, (
        f"普通容器（没有任何 annotation）起不来 —— wrapper 挡住了与 Neu Box "
        f"无关的容器:\n{(result.stdout or '')[:2000]}"
    )
    assert marker in (result.stdout or ""), (
        f"普通容器的输出不对:\n{(result.stdout or '')[:1000]}"
    )
    container.remove_container(reference)


def test_submit_docker_task_e2e(container, single_card, container_image):
    """提交 docker target 任务：HTTP → 调度 → Docker executor → 登记/授权 → 清理。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    node = single_card.device_node(device)
    marker = f"docker-task-{secrets.token_hex(4)}"
    command = (
        f"sh -c 'echo {marker}; sleep 2; "
        f"if : <{node}; then echo NODE_OPEN_OK; "
        f"else echo NODE_OPEN_DENIED; fi'"
    )

    task_id = single_card.submit(
        command,
        device_ids=[device],
        target={"type": "docker", "image": container_image},
    )
    task = single_card.wait_task(task_id, timeout=max(single_card.task_timeout, 180.0))
    assert task["status"] == "completed", (
        f"docker target 任务没有完成: {task}\n"
        f"{single_card.task_log_text(task_id)[:2000]}"
    )
    assert task["result"]["returncode"] == 0, task["result"]
    assert [_minor(item) for item in task["devices"]] == [device], task["devices"]

    text = single_card.task_log_text(task_id)
    assert marker in text, f"docker 任务日志缺少 marker:\n{text[:2000]}"
    assert "runtime 归属登记完成" in text, (
        f"DockerCommandExecutor 没有确认 runtime 登记；任务日志:\n{text[:2000]}"
    )
    assert "NODE_OPEN_OK" in text, (
        f"docker 任务里的设备节点打不开，登记/授权链路有问题:\n{text[:2000]}"
    )

    sandbox_name = f"sbx_{single_card.user}_{task_id}.slice"
    leftovers = single_card.docker(
        "ps", "-a",
        "--filter", f"label=neu-box.sandbox={sandbox_name}",
        "--format", "{{.ID}}",
    )
    assert not (leftovers.stdout or "").strip(), (
        f"docker target 任务结束后仍有容器残留:\n{leftovers.stdout}"
    )
    single_card.wait_idle_at_least(baseline)


def test_submit_docker_task_requires_device(container, container_image):
    """docker target 未申请设备必须被 API 拒绝。"""
    result = container.client.create_task(
        container.task_payload(
            "true", target={"type": "docker", "image": container_image},
        ),
    )
    assert result.status == 400, result.text
    error = result.value("error")
    assert "设备" in error or "device" in error.lower(), error


def test_container_exit_unregisters(container, single_card, container_image):
    """38 · 容器退出后登记被注销（注销不等于释放沙盒的设备）。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    node = single_card.device_node(device)
    terminal = single_card.spawn_terminal()
    sandbox = single_card.acquire_sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    name = sandbox["sandbox_name"]

    reference, result = run_container(
        single_card, container_image, annotation=name,
        command=container_probe_command(node),
    )
    assert result.returncode == 0, (result.stdout or "")[:2000]
    container_id = single_card.container_id_of(reference)
    assert single_card.wait_container_registered(container_id) == name

    stopped = single_card.docker("stop", "-t", "5", reference, timeout=90)
    assert stopped.returncode == 0, (stopped.stdout or "")[:2000]

    single_card.wait_container_unregistered(container_id)

    # 注销只撤掉"容器借用的授权"，沙盒自己仍然占着这张卡。
    record = single_card.find_sandbox(name)
    assert record is not None, (
        f"容器退出把沙盒 {name} 也带走了；沙盒只能由 release 或 Reaper 销毁"
    )
    assert record["state"] == "ACTIVE", record
    assert single_card.idle_devices() == baseline - 1, (
        f"容器退出后卡 {device} 就被放回了空闲池，但沙盒 {name} 还持有它"
    )

    single_card.remove_container(reference)
    single_card.release_sandbox(name)
    single_card.wait_idle_at_least(baseline)


def test_container_restart_registers_again(container, single_card, container_image):
    """39 · 容器重启后重复登记幂等。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    node = single_card.device_node(device)
    terminal = single_card.spawn_terminal()
    sandbox = single_card.acquire_sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    name = sandbox["sandbox_name"]

    reference, result = run_container(
        single_card, container_image, annotation=name,
        command=container_probe_command(node),
    )
    assert result.returncode == 0, (result.stdout or "")[:2000]
    container_id = single_card.container_id_of(reference)
    assert single_card.wait_container_registered(container_id) == name

    first_run = wait_container_log_count(
        single_card, reference, CONTAINER_PROBE_MARKER, 1,
    )
    assert CONTAINER_OPEN_OK in first_run, (
        f"重启前的探测就没打开设备节点，后面这条比不了:\n{first_run[:2000]}"
    )

    restarted = single_card.docker("restart", "-t", "5", reference, timeout=120)
    assert restarted.returncode == 0, (
        f"docker restart 失败:\n{(restarted.stdout or '')[:2000]}"
    )

    assert single_card.wait_container_registered(container_id) == name, (
        "容器重启后没有被重新登记 —— 重启会重新走一遍 runtime hook，登记要么"
        "命中已有记录（幂等），要么补一条新的"
    )
    text = wait_container_log_count(
        single_card, reference, CONTAINER_PROBE_MARKER, 2,
    )
    assert text.count(CONTAINER_OPEN_OK) >= 2, (
        f"重启后容器内设备节点打不开了（登记在、授权没恢复）:\n{text[:2000]}"
    )

    single_card.remove_container(reference)
    single_card.release_sandbox(name)
    single_card.wait_idle_at_least(baseline)
