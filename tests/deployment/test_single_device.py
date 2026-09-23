"""第 3 层 · 单卡（manifest 19-29）。

组 fixture ``single_card`` 已经确认至少有一张空闲设备且 /dev 下节点齐全 —— 这
一组不再判断"有没有卡"，全部当真跑，缺什么就直接失败。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import secrets
import time

import pytest

from deployment_support import (
    OPEN_DENIED,
    OPEN_INCONCLUSIVE,
    OPEN_OK,
    OPEN_TIMEOUT,
    process_alive,
)


def test_device_task_allocates_and_releases(single_card):
    """19 · 分配与完成后释放。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    marker = f"neu-box-device-{secrets.token_hex(4)}"

    task_id = single_card.submit(
        f"printf '%s-start\\n' {marker!r}; sleep 4; printf '%s-done\\n' {marker!r}",
        device_ids=[device],
    )
    running = single_card.wait_task_running(task_id)
    assert running["devices"], (
        f"申请了设备 {device} 的任务没有分配到任何设备: {running}"
    )
    assert len(running["devices"]) == 1, running["devices"]
    assert _minor(running["devices"][0]) == device, (
        f"指定设备 {device}，实际分配 {running['devices']}"
    )
    single_card.wait_idle_at_most(baseline - 1)

    task = single_card.wait_task(task_id)
    assert task["status"] == "completed", (
        f"设备任务未正常完成（状态 {task['status']}）: {task.get('result')}"
    )
    single_card.wait_idle_at_least(baseline)


def test_device_saturated_task_queues_instead_of_failing(single_card):
    """20 · 占满时排队而非失败。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    blocker = single_card.submit("sleep 8", device_ids=[device])
    single_card.wait_task_running(blocker)

    queued = single_card.submit("sleep 1", device_ids=[device])
    task = single_card.get_task(queued)
    assert task["status"] == "queued", (
        f"设备被占满时新任务状态为 {task['status']}，应当排队等待而不是失败: {task}"
    )
    entry = single_card.queue_entry(queued)
    assert entry is not None, f"排队中的任务没有出现在 /tasks 队列里: {queued}"
    assert entry["position"] >= 1, entry

    single_card.wait_task(blocker)
    assert single_card.wait_task(queued)["status"] == "completed", (
        "排队任务在设备释放后没有跑起来"
    )
    single_card.wait_idle_at_least(baseline)


def test_unknown_device_id_is_rejected(single_card):
    """21 · 申请不存在的卡号。"""
    missing = single_card.total_devices() + 999
    result = single_card.client.create_task(
        single_card.task_payload("true", device_ids=[missing]),
    )
    assert result.status == 400, (
        f"申请不存在的卡号 {missing} 返回 HTTP {result.status}，应为 400:\n"
        f"{result.text[:500]}"
    )
    error = result.value("error")
    assert str(missing) in error, f"错误信息没有指出是哪张卡: {error!r}"

    result = single_card.client.acquire(
        single_card.acquire_payload(_some_pid(single_card), device_ids=[missing]),
    )
    assert result.status == 400, (
        f"acquire 申请不存在的卡号 {missing} 返回 HTTP {result.status}，应为 400:\n"
        f"{result.text[:500]}"
    )


def test_device_num_beyond_total_is_rejected(single_card):
    """22 · 申请超过卡数的数量。"""
    total = single_card.total_devices()
    wanted = total + 1
    result = single_card.client.create_task(
        single_card.task_payload("true", device_num=wanted),
    )
    assert result.status == 400, (
        f"申请 {wanted} 张卡（共 {total} 张）返回 HTTP {result.status}，应为 400:\n"
        f"{result.text[:500]}"
    )
    error = result.value("error")
    assert "设备不足" in error, f"错误信息没有说明设备不足: {error!r}"


def test_same_device_not_handed_to_two_sandboxes(single_card):
    """23 · 同一张卡不同时给两个沙盒。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    first_terminal = single_card.spawn_terminal()
    second_terminal = single_card.spawn_terminal()

    # 第一个沙盒的 release 是这一条的被测行为（释放后排队者才拿得到卡），
    # 显式写在中间；with 只兜失败路径，出块时的释放此时已经是空操作。
    with single_card.sandbox(
        single_card.acquire_payload(first_terminal.pid, device_ids=[device]),
    ) as first:
        assert [int(item.split(":")[1]) for item in first["devices"]] == [device], first

        result = single_card.client.acquire(
            single_card.acquire_payload(second_terminal.pid, device_ids=[device]),
        )
        assert result.status == 202, (
            f"同一张卡被第二次申请时返回 HTTP {result.status}，应当排队（202）:\n"
            f"{result.text[:500]}"
        )
        request_id = result.value("acquire_id")

        # 排队不是"立刻失败"：连着查几次都必须还是 queued。
        for _ in range(3):
            polled = single_card.client.acquire_status(request_id)
            assert polled.status == 202, (
                f"排队中的 acquire 返回 HTTP {polled.status}，应当保持 202 queued:\n"
                f"{polled.text[:500]}"
            )
            assert polled.value("status") == "queued", polled.text
            time.sleep(single_card.poll)

        released = single_card.release_sandbox(first["sandbox_name"])
        assert released.status == 200, released.text

        second = _wait_acquire(single_card, request_id)
        try:
            assert [int(item.split(":")[1]) for item in second["devices"]] == [device], (
                f"卡释放后第二个申请拿到的设备不对: {second['devices']}"
            )
            assert second["sandbox_name"] != first["sandbox_name"], second
        finally:
            single_card.release_sandbox(second["sandbox_name"])
    single_card.wait_idle_at_least(baseline)


def test_concurrent_same_device_tasks_do_not_overlap(single_card):
    """并发提交同一张卡：调度/锁必须串行化，设备不能双重分配。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(single_card.submit, "sleep 2", device_ids=[device])
            for _ in range(4)
        ]
        task_ids = [future.result() for future in futures]

    tasks = [single_card.wait_task(task_id) for task_id in task_ids]
    for task in tasks:
        assert task["status"] == "completed", (
            f"并发任务没有正常完成: {task}\n"
            f"{single_card.task_log_text(task['task_id'])[:1000]}"
        )
        assert task["started_at"] is not None and task["finished_at"] is not None, task

    windows = sorted(
        (task["started_at"], task["finished_at"]) for task in tasks
    )
    for earlier, later in zip(windows, windows[1:]):
        assert not (later[0] < earlier[1]), (
            f"同一张卡上的两个任务时间窗重叠：{earlier} 与 {later}；"
            f"设备被同时分配给了两个沙盒"
        )

    single_card.wait_idle_at_least(baseline)


def test_release_returns_device_to_idle(single_card):
    """24 · release 后设备回 idle。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    terminal = single_card.spawn_terminal()

    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    ) as sandbox:
        single_card.wait_idle_at_most(baseline - 1)
        assert [int(item.split(":")[1]) for item in sandbox["devices"]] == [device], (
            sandbox
        )

        released = single_card.release_sandbox(sandbox["sandbox_name"])
        assert released.status == 200, released.text

        single_card.wait_sandbox_gone(sandbox["sandbox_name"])
    single_card.wait_idle_at_least(baseline)
    assert device in single_card.idle_minors(), (
        f"release 之后卡 {device} 没有回到空闲池: {single_card.idle_minors()}"
    )


def test_device_isolation_blocks_outside_open(single_card):
    """25 · 设备隔离：沙盒外 open 被阻断。"""
    if single_card.inside_sandbox():
        pytest.fail(
            "验收进程自己就在一个 Neu Box 沙盒里，没法充当沙盒外的对照组；"
            "请在部署机的普通会话（或 systemd-run 起的独立单元）里跑验收",
            pytrace=False,
        )

    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    node = single_card.device_node(device)

    _assert_open_state(
        single_card.probe_device_open(node), node,
        expect=OPEN_OK,
        context="设备空闲时从宿主机（根 cgroup）打开它",
        hint=(
            "这说明测试身份连未预留的设备节点都打不开 —— 是设备节点的权限位、"
            "驱动或 SELinux 之类的问题，不是隔离本身的问题"
        ),
    )

    hold = 10
    task_id = single_card.submit(
        single_card.probe_command(node, hold=hold), device_ids=[device],
    )
    single_card.wait_log_contains(task_id, "INSIDE_OPEN_OK", timeout=60)

    # 此刻 node 已被这个任务的沙盒预留：宿主机上的新进程必须被拒。
    _assert_open_state(
        single_card.probe_device_open(node), node,
        expect=OPEN_DENIED,
        context="沙盒持有设备时，从宿主机（根 cgroup）打开同一节点",
        hint=(
            "宿主机进程仍然能打开已被沙盒预留的设备 —— 设备预留没有生效，"
            "沙盒隔离形同虚设"
        ),
    )

    # 同一时刻，另一个"没有申请这张卡"的沙盒也必须被拒。
    assert single_card.get_task(task_id)["status"] == "running", (
        f"任务 {task_id} 在对照探测之前就结束了，{hold}s 的观察窗口不够用"
    )
    bystander = single_card.submit(single_card.probe_command(node), device_num=0)
    single_card.wait_log_contains(bystander, "INSIDE_OPEN_DENIED", timeout=30)

    task = single_card.wait_task(task_id)
    assert task["status"] == "completed", (
        f"设备隔离任务未正常完成（状态 {task['status']}）: {task.get('result')}\n"
        f"{single_card.task_log_text(task_id)[:2000]}"
    )
    assert [_minor(item) for item in task["devices"]] == [device], task["devices"]
    single_card.wait_idle_at_least(baseline)


def test_priority_jump_and_fifo(single_card):
    """26 · 高优先级插队 + 同级 FIFO。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    # 四条都指定同一张卡，排位就只由 priority 和提交顺序决定（否则 position
    # 会掺进"某个任务其实根本拿不到这张卡"的干扰）。
    blocker = single_card.submit("sleep 8", device_ids=[device])
    single_card.wait_task_running(blocker)

    normal = single_card.submit("sleep 1", device_ids=[device], priority=0)
    high_first = single_card.submit("sleep 1", device_ids=[device], priority=1)
    high_second = single_card.submit("sleep 1", device_ids=[device], priority=1)

    entries = {}
    for task_id in (normal, high_first, high_second):
        entry = single_card.queue_entry(task_id)
        assert entry is not None, f"任务 {task_id} 不在 /tasks 队列里"
        entries[task_id] = entry
    assert entries[high_first]["priority"] == 1, entries[high_first]
    assert entries[high_second]["priority"] == 1, entries[high_second]
    assert entries[normal]["priority"] == 0, entries[normal]
    assert (
        entries[high_first]["position"]
        < entries[high_second]["position"]
        < entries[normal]["position"]
    ), (
        f"优先级排位错误：高1={entries[high_first]['position']}，"
        f"高2={entries[high_second]['position']}，普通={entries[normal]['position']}"
    )

    started = {}
    for task_id in (blocker, high_first, high_second, normal):
        task = single_card.wait_task(task_id)
        assert task["status"] == "completed", (
            f"优先级用例的任务 {task_id} 状态为 {task['status']}: {task.get('result')}"
        )
        started[task_id] = task["started_at"]
    assert started[high_first] < started[high_second] < started[normal], (
        f"实际启动顺序错误：高1={started[high_first]}，"
        f"高2={started[high_second]}，普通={started[normal]}"
    )
    single_card.wait_idle_at_least(baseline)


def test_cancel_running_task_releases_device(single_card):
    """27 · 运行中取消 → 设备释放。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    task_id = single_card.submit("sleep 300", device_ids=[device])
    single_card.wait_task_running(task_id)

    result = single_card.client.delete_tasks([task_id])
    assert result.status == 200, result.text
    assert result.value("deleted") == 1, result.text

    task = single_card.wait_task(task_id)
    assert task["status"] == "cancelled", (
        f"取消后的任务状态应为 cancelled（取消是独立终态，不再混在 failed 里），"
        f"实际为 {task['status']}"
    )
    assert "手动取消" in (task["result"].get("error") or ""), (
        f"取消原因不明确: {task['result']}"
    )
    single_card.wait_idle_at_least(baseline)
    assert device in single_card.idle_minors(), (
        f"取消之后卡 {device} 没有回到空闲池: {single_card.idle_minors()}"
    )


def test_acquire_list_release_round_trip(single_card):
    """28 · acquire / list / release 公开接口。

    这一轮的低层语义是"借一个终端"：acquire 把调用方的进程借进沙盒，
    release 时归还。容器**不再**通过 join 进沙盒（它走 OCI runtime hook 登记，
    借用沙盒的授权但不住在沙盒 cgroup 里），所以这里只验终端的借还。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    terminal = single_card.spawn_terminal()
    origin = single_card.process_cgroup(terminal.pid)
    assert origin, f"读不到测试进程 {terminal.pid} 的初始 cgroup"

    # release 本身是被测行为，写在中间；with 保证前面任何断言失败都不漏卡。
    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    ) as sandbox:
        name = sandbox["sandbox_name"]
        assert name.startswith(f"sbx_{single_card.user}_"), (
            f"沙盒命名与用户名对不上: {name}"
        )
        assert [int(item.split(":")[1]) for item in sandbox["devices"]] == [device], (
            sandbox
        )

        record = single_card.find_sandbox(name)
        assert record is not None, f"acquire 之后 /sandbox/list 里没有 {name}"
        assert record["owner"] == single_card.user, record
        assert record["state"] == "ACTIVE", (
            f"acquire 之后沙盒状态应为 ACTIVE，实际为 {record['state']!r}"
        )
        assert terminal.pid in record["pids"], (
            f"借出的终端 {terminal.pid} 不在沙盒的 PID 列表里: {record['pids']}"
        )
        assert [int(item.split(":")[1]) for item in record["devices"]] == [device], (
            record
        )

        by_pid = single_card.sandbox_of_pid(terminal.pid)
        assert by_pid["sandbox_name"] == name, by_pid
        assert single_card.process_cgroup(terminal.pid).endswith(f"sandbox_{name}"), (
            f"终端没有进入沙盒 cgroup: {single_card.process_cgroup(terminal.pid)}"
        )

        released = single_card.release_sandbox(name)
        assert released.status == 200, released.text
        single_card.wait_sandbox_gone(name)

        assert process_alive(terminal.pid), (
            f"release 误杀了借出去的终端 {terminal.pid}"
        )
        back = single_card.process_cgroup(terminal.pid)
        assert back == origin and "sandbox_" not in back, (
            f"release 后终端没有回到原 cgroup：期望 {origin}，实际 {back}"
        )
    single_card.wait_idle_at_least(baseline)


def test_memory_limit_blocks_overcommit(single_card):
    """29 · 内存上限真的挡住超额分配。"""
    baseline = single_card.idle_devices()
    # dd 在读之前会真的分配 bs 大小的缓冲区：256 MiB 在 128 MiB 上限下必然
    # 失败（malloc 直接失败，或写页触发 cgroup OOM kill），两条路径都非零退出。
    task_id = single_card.submit(
        "dd if=/dev/zero of=/dev/null bs=256M count=1",
        device_ids=[single_card.idle_minors()[0]], memory=128, mem_unit="MB",
    )
    task = single_card.wait_task(task_id)
    assert task["status"] == "failed", (
        f"256 MiB 的分配在 128 MiB 上限下仍然成功了（状态 {task['status']}）: "
        f"{task.get('result')}"
    )
    assert task["result"]["returncode"] != 0, task["result"]
    single_card.wait_idle_at_least(baseline)


def test_release_keeps_the_caller_inside_sandbox(single_card):
    """55 · 从沙盒里发起 release 时，调用方不能被这次销毁带走。

    用户实测的路径：``neubox release`` 是 acquire 借出去的那个 shell fork 出来的
    子进程 —— cgroup 成员身份随 fork 继承，所以它住在沙盒 cgroup 里，却没有
    origin；而销毁的最后一步是 ``cgroup.kill``，于是它会把自己一起杀掉
    （``zsh: killed``、退出码 137，命令不返回、脚本串联直接断）。

    修法是 release 请求带上 ``host_pid``，Worker 在销毁前把**它自己**搬回父进程
    的 origin；它的兄弟进程（同样是"沙盒里长出来的"）照旧被收掉 —— 这一条正反
    两面都要验，否则把孤儿进程放生了也一样是 bug。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    tag = f"caller-{secrets.token_hex(4)}"
    terminal, _ = single_card.fork_child_in_place(seconds=600, tag=tag, count=2)

    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    ) as sandbox:
        name = sandbox["sandbox_name"]
        caller, orphan = single_card.fork_children(tag=tag, count=2)
        for pid in (caller, orphan):
            assert single_card.process_cgroup(pid).endswith(f"sandbox_{name}"), (
                f"子进程 {pid} 没有落在沙盒 cgroup 里 "
                f"({single_card.process_cgroup(pid)})，这一条的前提不成立"
            )

        released = single_card.client.release(
            name, host_pid=caller, timeout=single_card.task_timeout,
        )
        assert released.status == 200, released.text
        single_card.wait_sandbox_gone(name)

        assert process_alive(caller), (
            f"调用方 PID {caller} 被这次 release 带走了 —— neubox release 就会"
            f"看到 zsh: killed / 退出码 137，host_pid 没起作用"
        )
        assert process_alive(terminal.pid), (
            f"借出去的终端 {terminal.pid} 没有迁回原 cgroup（或被杀）"
        )
        single_card.wait_process_gone(orphan)

    single_card.wait_idle_at_least(baseline)


# ── 小工具 ──────────────────────────────────────────────────────


def _minor(device: str) -> int:
    return int(str(device).split(":")[-1])


def _some_pid(deployment) -> int:
    return deployment.spawn_terminal().pid


def _wait_acquire(deployment, request_id: str) -> dict:
    deadline = time.time() + deployment.task_timeout
    while time.time() < deadline:
        result = deployment.client.acquire_status(request_id)
        if result.status == 201:
            body = result.json()
            # 登记进会话收尾：这条路径绕过了 acquire_sandbox()，不记下来
            # 失败时就没人知道还有个沙盒在占卡。
            deployment.created_sandboxes.append(body.get("sandbox_name", ""))
            return body
        if result.status == 202:
            time.sleep(deployment.poll)
            continue
        pytest.fail(
            f"排队的 acquire {request_id} 最终失败（HTTP {result.status}）:\n"
            f"{result.text[:500]}",
            pytrace=False,
        )
    pytest.fail(
        f"排队的 acquire {request_id} 在 {deployment.task_timeout:.0f}s 内没有拿到设备",
        pytrace=False,
    )


def _assert_open_state(state: int, node: str, *, expect: int, context: str,
                       hint: str) -> None:
    if state == expect:
        return
    names = {
        OPEN_OK: "打开成功",
        OPEN_DENIED: "被拒绝（EPERM/EACCES）",
        OPEN_INCONCLUSIVE: "无从判断（非权限类错误）",
        OPEN_TIMEOUT: "超时（10s）",
    }
    pytest.fail(
        f"{context}：结果是 {names.get(state, state)}，期望 {names[expect]}"
        f"（节点 {node}）。{hint}",
        pytrace=False,
    )
