"""第 3 层 · 调度（manifest 52-53、56-57、76-80）。

这一组验的是"卡什么时候发出去"，而不是卡本身：优先级只分两档（0 普通 /
1 赶论文），同一档内按提交顺序；每轮调度扫一遍队列，**取第一个能分配的任务**
—— 队首要的卡没空，不能把后面本来跑得动的任务一起挡住。调度顺序的纯逻辑在
``tests/unit/test_scheduler_order.py`` 里逐条覆盖，这里验的是同一套语义在真
Worker + 真卡上的表现。
"""

from __future__ import annotations

import time

import pytest


def _minor(device: str) -> int:
    return int(str(device).split(":")[-1])


def test_priority_does_not_leave_a_free_device_idle(multi_card):
    """52 · 高优先级要的卡被占，不能让另一张空闲卡陪着等。"""
    first, second = multi_card.require_idle(2)
    baseline = multi_card.idle_devices()

    blocker = multi_card.submit("sleep 20", device_ids=[first])
    multi_card.wait_task_running(blocker)

    high = multi_card.submit("sleep 1", device_ids=[first], priority=1)
    low = multi_card.submit("sleep 1", device_ids=[second], priority=0)

    low_task = multi_card.wait_task(low)
    assert low_task["status"] == "completed", (
        f"卡 {second} 一直空着，低优先级任务 {low} 却没跑起来 —— 调度把整条队列"
        f"堵在高优先级那条后面了: {low_task}"
    )
    assert [_minor(item) for item in low_task["devices"]] == [second], low_task
    entry = multi_card.queue_entry(high)
    assert entry is not None and entry["status"] == "queued", (
        f"卡 {first} 还被 {blocker} 占着，高优先级任务 {high} 不该起: {entry}"
    )

    multi_card.client.delete_tasks([blocker])
    multi_card.wait_task(blocker)
    high_task = multi_card.wait_task(high)
    assert high_task["status"] == "completed", high_task.get("result")
    multi_card.wait_idle_at_least(baseline)


def test_acquire_shares_the_task_priority_order(multi_card):
    """53 · acquire 和 task 用同一套优先级：高优先级 acquire 先拿卡。"""
    first, second = multi_card.require_idle(2)
    baseline = multi_card.idle_devices()

    blocker = multi_card.submit("sleep 20", device_ids=[first])
    multi_card.wait_task_running(blocker)

    # 同一条卡上：先排一个普通任务，再排一个高优先级 acquire。
    normal = multi_card.submit("sleep 1", device_ids=[first], priority=0)
    terminal = multi_card.spawn_terminal()
    queued = multi_card.client.acquire(
        multi_card.acquire_payload(
            terminal.pid, device_ids=[first], priority=1,
        ),
    )
    assert queued.status == 202, (
        f"卡被占着时 acquire 应当排队（202），实际 HTTP {queued.status}:\n"
        f"{queued.text[:500]}"
    )
    request_id = queued.value("acquire_id")

    multi_card.client.delete_tasks([blocker])
    multi_card.wait_task(blocker)

    # 卡空出来了：高优先级的 acquire 该先拿到，任务继续排队。
    deadline = time.time() + multi_card.task_timeout
    sandbox = None
    while time.time() < deadline:
        polled = multi_card.client.acquire_status(request_id)
        if polled.status == 201:
            sandbox = polled.json()
            break
        assert polled.status == 202, (
            f"acquire {request_id} 异常状态 HTTP {polled.status}: {polled.text[:500]}"
        )
        entry = multi_card.queue_entry(normal)
        assert entry is not None and entry["status"] == "queued", (
            f"高优先级 acquire 还没落地，普通任务 {normal} 就已经拿到卡了: {entry}"
        )
        time.sleep(multi_card.poll)
    assert sandbox is not None, (
        f"高优先级 acquire {request_id} 在 {multi_card.task_timeout:.0f}s 内没拿到卡"
    )

    released = multi_card.release_sandbox(sandbox["sandbox_name"])
    assert released.status == 200, released.text
    task = multi_card.wait_task(normal)
    assert task["status"] == "completed", task.get("result")
    multi_card.wait_idle_at_least(baseline)


def test_queued_acquire_shows_up_in_the_unified_queue(single_card):
    """56 · 排队中的 acquire 会出现在统一队列视图里（带 kind/position/state）。

    这是 master 那个列表以前看不到终端会话的根因：`/tasks` 只查 tasks 表，而
    acquire 既没有行（排队中）也不在里面（拿到卡后）。现在两类条目合并成一个
    视图，排队中的会话也带着 position 出现。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    blocker = single_card.submit("sleep 20", device_ids=[device])
    single_card.wait_task_running(blocker)

    terminal = single_card.spawn_terminal()
    queued = single_card.client.acquire(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    assert queued.status == 202, queued.text
    request_id = queued.value("acquire_id")

    item = single_card.queue_item(request_id, kind="acquire")
    assert item is not None, (
        f"排队中的 acquire {request_id} 没有出现在 /tasks 统一视图里："
        f"{single_card.queue()}"
    )
    assert item["kind"] == "acquire", item
    assert item["status"] == "queued", item
    assert item["position"] >= 1, item
    assert item["device_num"] == 0 and item["devices"] == [], item
    assert "command" not in item, (
        f"acquire 条目不该带上 command 之类的任务字段: {sorted(item)}"
    )
    # 同一条队列里也还能看到任务，而且两类一起编号 —— 至少 blocker 在里面。
    assert single_card.queue_item(blocker) is not None

    cancelled = single_card.client.cancel_entry(request_id, kind="acquire")
    assert cancelled.status == 200, cancelled.text
    assert cancelled.json().get("status") == "cancelled", cancelled.text

    single_card.client.delete_tasks([blocker])
    single_card.wait_task(blocker)
    single_card.wait_idle_at_least(baseline)


def test_card_less_tasks_are_not_blocked_by_a_queued_card_task(multi_card):
    """76 · 队首要卡但卡被占时，不占卡的任务照样跑。

    队列分两档（要卡的 / 不要卡的），每轮扫一遍、取第一个能分配的。这条盯的是
    档位边界：前一档里排着一条等卡的，不能把后一档一起拖住 —— 它是"卡不够时
    机器仍然能被用来写代码/搬数据"的全部理由。
    """
    first = multi_card.require_idle(1)[0]
    baseline = multi_card.idle_devices()

    blocker = multi_card.submit("sleep 10", device_ids=[first])
    multi_card.wait_task_running(blocker)

    blocked = multi_card.submit("sleep 1", device_ids=[first])
    card_less = [
        multi_card.submit(f"printf 'bucket0-{index}\\n'", device_num=0)
        for index in range(3)
    ]
    entry = multi_card.queue_entry(blocked)
    assert entry is not None and entry["status"] == "queued", entry

    for task_id in card_less:
        task = multi_card.wait_task(task_id, timeout=60)
        assert task["status"] == "completed", (task_id, task.get("result"))
        assert task.get("devices") == [], task

    entry = multi_card.queue_entry(blocked)
    assert entry is not None and entry["status"] == "queued", (
        f"不占卡的任务都跑完了，卡还被 {blocker} 占着，{blocked} 应当还在排队: {entry}"
    )

    multi_card.client.delete_tasks([blocker])
    multi_card.wait_task(blocker)
    assert multi_card.wait_task(blocked)["status"] == "completed"
    multi_card.wait_idle_at_least(baseline)


def test_priority_multi_card_request_is_not_half_allocated(multi_card):
    """77 · 高优先级要 2 张、只有 1 张空：整单排队，不许占住那张空卡。

    45/46 验的是同一条规则的单卡版本；这条加上"高优先级"，盯的是最容易被写漏的
    组合分支：整单分配失败时既不能半分配，也不能把仅剩的空卡预占住。

    判据必须只依赖**我们自己**的占位：早先这版想把"除一张以外的空闲卡全占住"
    来制造"只有 1 张空"，但机器的空闲卡是活的 —— 有外部作业在跑时，它中途结束会
    凭空多出一张空卡，高优先级那条就真的起来了（真机踩过两次）。所以改成显式
    指定要哪两张：其中一张被我们自己的 blocker 占着，只有删掉它才可能满足。
    """
    free, busy = multi_card.require_idle(2)
    baseline = multi_card.idle_devices()

    # 占位任务要明显长于这条用例的耗时（sleep 10 时它会在中途自己结束）。
    blocker = multi_card.submit("sleep 120", device_ids=[busy])
    multi_card.wait_task_running(blocker)
    multi_card.wait_idle_at_most(baseline - 1)

    high = multi_card.submit(
        "sleep 1", device_num=2, device_ids=[free, busy], priority=1,
    )
    low = multi_card.submit("sleep 1", device_ids=[free], priority=0)

    entry = multi_card.queue_entry(high)
    assert entry is not None and entry["status"] == "queued", entry
    assert entry["devices"] == [], f"整单排队的任务不该预占设备: {entry}"

    low_task = multi_card.wait_task(low, timeout=60)
    assert low_task["status"] == "completed", low_task.get("result")
    assert [_minor(item) for item in low_task["devices"]] == [free], low_task

    entry = multi_card.queue_entry(high)
    assert entry is not None and entry["status"] == "queued", (
        f"卡 {busy} 还被 {blocker} 占着，要 [{free}, {busy}] 的高优先级任务 "
        f"{high} 不该起: {entry}"
    )
    assert entry["devices"] == [], entry

    multi_card.client.delete_tasks([blocker])
    multi_card.wait_task(blocker)
    high_task = multi_card.wait_task(high)
    assert high_task["status"] == "completed", (
        "两张卡都空出来之后，整单排队的任务还没跑"
    )
    assert sorted(_minor(item) for item in high_task["devices"]) == sorted([free, busy])
    multi_card.wait_idle_at_least(baseline)


def test_queue_positions_stay_contiguous_after_cancel(single_card):
    """78 · 队列账：取消一条排队条目之后 position 连续、顺序不变。

    `position` 是调度器每轮重算后写进统一视图的（master 就靠它显示队列）。
    取消/释放之后留下空洞或重复，界面上看到的队列就是错的。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    blocker = single_card.submit("sleep 10", device_ids=[device])
    single_card.wait_task_running(blocker)

    terminal = single_card.spawn_terminal()
    queued = single_card.client.acquire(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    assert queued.status == 202, queued.text
    request_id = queued.value("acquire_id")

    task_id = single_card.submit("printf 'queued\\n'", device_ids=[device])

    def queued_entries() -> list[tuple]:
        return [
            (entry.get("kind"), entry.get("id"), entry.get("position"))
            for entry in single_card.queue()
            if entry.get("status") == "queued"
        ]

    before = queued_entries()
    assert [item[1] for item in before] == [request_id, task_id], before
    positions = [item[2] for item in before]
    assert positions == list(range(positions[0], positions[0] + len(positions))), (
        f"排队条目的 position 不连续: {before}"
    )

    cancelled = single_card.client.cancel_entry(request_id, kind="acquire")
    assert cancelled.status == 200, cancelled.text

    after = queued_entries()
    assert [item[1] for item in after] == [task_id], after
    assert after[0][2] == positions[0], (
        f"取消之后后面那条没有顶上前一个位置（留下空洞）: 前={before} 后={after}"
    )

    single_card.client.delete_tasks([blocker])
    single_card.wait_task(blocker)
    assert single_card.wait_task(task_id)["status"] == "completed"
    single_card.wait_idle_at_least(baseline)


def test_many_equal_priority_tasks_keep_submission_order(single_card):
    """79 · 同优先级 8 条任务严格按提交顺序上卡（FIFO 稳定性）。

    26 号只比了两条。队列是有序字典 + 每轮重算 position，条目一多，任何"用集合
    迭代顺序/漏排序"的写法都会在这里露出来 —— 表现出来就是任务乱序启动。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    tasks = [single_card.submit("sleep 0.3", device_ids=[device])
             for _ in range(8)]
    started = []
    for task_id in tasks:
        task = single_card.wait_task(task_id, timeout=120)
        assert task["status"] == "completed", (task_id, task.get("result"))
        started.append(task["started_at"])

    assert started == sorted(started), (
        f"同优先级任务的启动顺序不是提交顺序：{started}"
    )
    assert len(set(started)) == len(started), (
        f"有任务的时间戳完全相同，顺序无法判定：{started}"
    )
    single_card.wait_idle_at_least(baseline)


def test_task_before_acquire_keeps_fifo_across_kinds(single_card):
    """80 · 同优先级下 task 和 acquire 交错时也按提交顺序拿卡。

    53 号验的是"高优先级 acquire 插队"，这条验它的补集：同优先级时跨 kind 也走
    同一条 FIFO —— 先提交的 task 拿到卡并跑完，后提交的 acquire 才落地。两类
    条目混在一条队列里，顺序一旦按 kind 分家，用户看到的就是"我明明先提交的"。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    blocker = single_card.submit("sleep 10", device_ids=[device])
    single_card.wait_task_running(blocker)

    task_id = single_card.submit("printf 'fifo-task\\n'", device_ids=[device])
    terminal = single_card.spawn_terminal()
    queued = single_card.client.acquire(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    assert queued.status == 202, queued.text
    request_id = queued.value("acquire_id")

    entries = {
        entry.get("id"): entry
        for entry in single_card.queue()
        if entry.get("status") == "queued"
    }
    assert task_id in entries and request_id in entries, entries
    assert entries[task_id]["position"] < entries[request_id]["position"], entries

    single_card.client.delete_tasks([blocker])
    single_card.wait_task(blocker)
    assert single_card.wait_task(task_id, timeout=60)["status"] == "completed", (
        "先提交的 task 没有先拿到卡"
    )

    landed = None
    deadline = time.time() + 60
    while time.time() < deadline:
        polled = single_card.client.acquire_status(request_id)
        if polled.status == 201:
            landed = polled.json()
            break
        time.sleep(single_card.poll)
    assert landed is not None, (
        f"先提交的 task 已经跑完，后提交的 acquire {request_id} 还没落地"
    )
    sandbox = landed.get("sandbox_name") or ""
    assert sandbox, landed
    single_card.client.release(sandbox)
    single_card.wait_sandbox_gone(sandbox)
    single_card.wait_idle_at_least(baseline)


def test_cancel_pending_acquire_leaves_nothing_behind(single_card):
    """57 · 取消排队中的 acquire：队列干净、卡数不变、没有残留沙盒。

    对应 `neubox acquire` 阻塞期间按 Ctrl-C 的那条路径：请求还没派发就被摘掉，
    既不会建沙盒，也不会留下需要 release 的东西。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    blocker = single_card.submit("sleep 20", device_ids=[device])
    single_card.wait_task_running(blocker)
    # 记账基线要在 blocker 占上卡之后再取：它本来就占着这 1 张。
    baseline_while_blocked = single_card.idle_devices()
    sandboxes_before = {item["name"] for item in single_card.sandboxes()}

    terminal = single_card.spawn_terminal()
    queued = single_card.client.acquire(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    assert queued.status == 202, queued.text
    request_id = queued.value("acquire_id")

    cancelled = single_card.client.cancel_entry(request_id, kind="acquire")
    assert cancelled.status == 200, cancelled.text
    assert cancelled.json().get("status") == "cancelled", cancelled.text

    history = single_card.queue_item(request_id, kind="acquire")
    assert history is not None and history["status"] == "cancelled", (
        f"取消之后这个 acquire 应当以 cancelled 留痕: {history}"
    )
    still_active = [
        entry for entry in single_card.queue(kind="acquire")
        if entry["status"] in ("queued", "allocating", "active")
        and entry["id"] == request_id
    ]
    assert still_active == [], f"取消之后它不该还在活动队列里: {still_active}"

    # 重复取消：幂等（404 或明确的状态，不该抛）
    again = single_card.client.cancel_entry(request_id, kind="acquire")
    assert again.status in (200, 404), again.text

    assert single_card.idle_devices() == baseline_while_blocked, (
        f"取消排队 acquire 不该改变卡数：{single_card.idle_devices()} != "
        f"{baseline_while_blocked}（blocker 占着 1 张）"
    )
    assert {item["name"] for item in single_card.sandboxes()} == sandboxes_before, (
        f"取消排队 acquire 不该建沙盒：{single_card.sandboxes()}"
    )

    single_card.client.delete_tasks([blocker])
    single_card.wait_task(blocker)
    single_card.wait_idle_at_least(baseline)
