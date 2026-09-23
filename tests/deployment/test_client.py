"""第 3 层 · client(neubox) 的基本路径（manifest 58-60、65-66）。

这一组用**真 neubox 二进制**打真 Worker：别的组都绕过 client 直接调 HTTP / docker
CLI，所以 client 自己的参数拼装、退出码、轮询与取消逻辑一直没有真机覆盖（历史上
`docker run` 少传 argv[0]、`release` 把自己杀掉，都是只在这条路径上出现的）。

容器路径（`neubox docker run` 与 stop / start / release / exec 生命周期）单独一组：
它要 dockerd + `neu-box-runtime`，见 `test_client_docker.py`。这一组只要卡。

前置是软缺失：``neubox`` 没装就整组跳过并打印原因；装了但版本不够直接失败。
"""

from __future__ import annotations

import re
import secrets
import signal
import subprocess
import time

from deployment_support import neubox_cli


def _neubox(neubox_bin: str, *args: str, timeout: float = 120.0) -> str:
    """跑一条 neubox 命令；非 0 退出直接把 stdout/stderr 贴进失败信息。"""
    return neubox_cli(neubox_bin, *args, timeout=timeout)


def test_client_acquire_list_release_roundtrip(neubox_bin, single_card):
    """58 · 真 neubox：acquire → list → release 走通。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    terminal = single_card.spawn_terminal()

    output = _neubox(
        neubox_bin, "acquire", "--pid", str(terminal.pid),
        "--device", str(device),
    )
    match = re.search(r"(sbx_\S+\.slice)", output)
    assert match, f"neubox acquire 的输出里没有沙盒名:\n{output}"
    sandbox = match.group(1)

    listing = _neubox(neubox_bin, "list")
    assert sandbox in listing, f"neubox list 里看不到刚建的沙盒:\n{listing}"

    _neubox(neubox_bin, "release", sandbox)
    single_card.wait_sandbox_gone(sandbox)
    assert single_card.idle_devices() == baseline, (
        f"release 之后卡没有全部回到空闲池: {single_card.idle_devices()} != {baseline}"
    )


def test_client_submit_and_result(neubox_bin, single_card):
    """59 · 真 neubox：submit 一个不占卡的任务并读到结果。"""
    marker = f"neubox-client-{secrets.token_hex(4)}"
    output = _neubox(
        neubox_bin, "submit", "--device-num", "0", "--",
        "sh", "-c", f"echo {marker}",
    )
    match = re.search(r"\b([0-9a-f]{12})\b", output)
    assert match, f"neubox submit 的输出里没有 task_id:\n{output}"
    task_id = match.group(1)

    result = _neubox(neubox_bin, "result", task_id)
    assert marker in result, f"结果里没有任务输出 {marker}:\n{result}"


def test_client_cancel_queued_task(neubox_bin, single_card):
    """60 · 真 neubox：取消一个排队中的任务（取消是独立终态、留痕）。"""
    device = single_card.idle_minors()[0]
    blocker = single_card.submit("sleep 30", device_ids=[device])
    single_card.wait_task_running(blocker)

    try:
        output = _neubox(
            neubox_bin, "submit", "--devices", str(device), "--",
            "sh", "-c", "echo should-not-run",
        )
        match = re.search(r"\b([0-9a-f]{12})\b", output)
        assert match, f"neubox submit 的输出里没有 task_id:\n{output}"
        queued = match.group(1)

        cancelled = _neubox(neubox_bin, "cancel", queued)
        assert "cancel" in cancelled.lower() or "取消" in cancelled, cancelled

        result = _neubox(neubox_bin, "result", queued)
        assert "cancelled" in result, (
            f"取消后的任务状态应当是 cancelled（留痕），实际:\n{result}"
        )
    finally:
        single_card.client.delete_tasks([blocker])
        single_card.wait_task(blocker)


def test_client_cancel_running_task_releases_device(neubox_bin, single_card):
    """65 · 真 neubox：取消**运行中**的任务 → 终态 cancelled、卡回空闲池。"""
    baseline = single_card.idle_devices()
    device = single_card.require_idle(1)[0]

    output = _neubox(
        neubox_bin, "submit", "--devices", str(device), "--",
        "sh", "-c", "sleep 300",
    )
    match = re.search(r"\b([0-9a-f]{12})\b", output)
    assert match, f"neubox submit 的输出里没有 task_id:\n{output}"
    task_id = match.group(1)
    single_card.wait_task_running(task_id)

    cancelled = _neubox(neubox_bin, "cancel", task_id)
    assert "cancel" in cancelled.lower() or "取消" in cancelled, cancelled

    task = single_card.wait_task(task_id)
    assert task["status"] == "cancelled", (
        f"取消后的任务状态应当是 cancelled，实际 {task['status']}: {task}"
    )
    single_card.wait_idle_at_least(baseline)
    assert device in single_card.idle_minors(), (
        f"取消运行中任务后卡 {device} 没有回到空闲池: {single_card.idle_minors()}"
    )


def test_client_acquire_sigint_cancels_queued_acquire(neubox_bin, single_card):
    """66 · 真 neubox：排队期间按 Ctrl-C → 只发一次取消、退出 130、队列干净。

    这是 `neubox acquire` 阻塞轮询那条路上的真机回归：客户端收到 SIGINT 后发一个
    取消请求，worker 内部决定"摘出队列"还是"就地释放"，客户端不补第二次调用。
    """
    baseline = single_card.idle_devices()
    device = single_card.require_idle(1)[0]

    blocker = single_card.submit("sleep 30", device_ids=[device])
    single_card.wait_task_running(blocker)

    terminal = single_card.spawn_terminal()
    process = subprocess.Popen(
        [neubox_bin, "acquire", "--pid", str(terminal.pid),
         "--device", str(device)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # 等到它在 worker 侧真的排上了队（统一视图里能看到 queued 的会话）。
        deadline = time.time() + 30.0
        request_id = None
        while time.time() < deadline:
            pending = [
                entry for entry in single_card.queue(kind="acquire")
                if entry["status"] == "queued"
            ]
            if pending:
                request_id = pending[0]["id"]
                break
            time.sleep(0.25)
        assert request_id, (
            f"30s 内没看到排队中的 acquire（卡 {device} 应当被 blocker 占着）"
        )

        process.send_signal(signal.SIGINT)
        out, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        single_card.client.delete_tasks([blocker])
        single_card.wait_task(blocker)

    assert process.returncode == 130, (
        f"Ctrl-C 之后应当以 130 退出（128+SIGINT），实际 {process.returncode}；"
        f"输出：\n{out[:1000]}"
    )
    assert "取消" in out, f"输出里没有取消提示：\n{out[:1000]}"

    entry = single_card.queue_item(request_id, kind="acquire")
    assert entry is not None and entry["status"] == "cancelled", (
        f"取消之后这个 acquire 应当以 cancelled 留痕，实际 {entry}"
    )
    assert single_card.idle_devices() == baseline, (
        f"取消排队 acquire 不该改变卡数：{single_card.idle_devices()} != {baseline}"
    )
