"""第 3 层 · 多卡（manifest 30-31、44-50）。

组 fixture ``multi_card`` 已经确认至少有两张空闲设备；这里不再判断卡数。
选卡一律走 ``require_idle(n)``：Worker 的 ``/status`` 已经把外部占用算进去
了，用例不假设哪张卡是好的（维护窗口 8 张全空闲时自然拿到 8 张）。
"""

from __future__ import annotations

import time

import pytest


def _minor(device: str) -> int:
    return int(str(device).split(":")[-1])


def _devices_of(task: dict) -> list[int]:
    return [_minor(item) for item in task.get("devices") or []]


def test_two_devices_run_concurrently(multi_card):
    """30 · 两张不同卡并发。"""
    baseline = multi_card.idle_devices()
    idle = multi_card.idle_minors()
    first, second = idle[0], idle[1]

    first_task = multi_card.submit("sleep 8", device_ids=[first])
    second_task = multi_card.submit("sleep 8", device_ids=[second])

    first_running = multi_card.wait_task_running(first_task)
    second_running = multi_card.wait_task_running(second_task)
    assert _devices_of(first_running) == [first], first_running["devices"]
    assert _devices_of(second_running) == [second], second_running["devices"]

    # 第二条任务必须在第一条结束之前就已经起来了，否则不叫并发。
    multi_card.wait_idle_at_most(baseline - 2)

    first_done = multi_card.wait_task(first_task)
    second_done = multi_card.wait_task(second_task)
    assert first_done["status"] == "completed", first_done.get("result")
    assert second_done["status"] == "completed", second_done.get("result")
    assert second_done["started_at"] < first_done["finished_at"], (
        f"第二条任务在第一条结束之后才启动：second.started_at="
        f"{second_done['started_at']}，first.finished_at={first_done['finished_at']}；"
        f"两张卡上的任务并没有真的并发"
    )
    multi_card.wait_idle_at_least(baseline)


def test_single_task_takes_two_devices(multi_card):
    """31 · 单任务两张指定卡。"""
    baseline = multi_card.idle_devices()
    idle = multi_card.idle_minors()
    first, second = idle[0], idle[1]

    task_id = multi_card.submit("sleep 4", device_ids=[first, second])
    running = multi_card.wait_task_running(task_id)
    devices = _devices_of(running)
    assert devices == [first, second], (
        f"多卡任务应同时持有 {first}/{second}，实际为 {devices}"
    )
    # device_ids 非空时 device_num 归 0 —— "要了几张卡"由 devices 回答，
    # device_num 只表示"不给 ids 时自动分几张"（docs/worker-api.md）。
    assert running["device_num"] == 0, (
        f"指定 device_ids 的任务其 device_num 应为 0（申请数看 devices），"
        f"实际为 {running['device_num']!r}"
    )
    multi_card.wait_idle_at_most(baseline - 2)

    task = multi_card.wait_task(task_id)
    assert task["status"] == "completed", task.get("result")
    multi_card.wait_idle_at_least(baseline)


def test_auto_allocation_reports_device_num(multi_card):
    """44 · 不给 ids 时按 device_num 自动分配，device_num 就是申请数量。"""
    multi_card.wait_idle_at_least(2)
    baseline = multi_card.idle_devices()

    task_id = multi_card.submit("sleep 2", device_num=2)
    running = multi_card.wait_task_running(task_id)
    devices = _devices_of(running)
    assert len(devices) == 2, f"device_num=2 应分到两张卡，实际为 {devices}"
    assert sorted(devices) == sorted(set(devices)), f"两次分到同一张卡: {devices}"
    assert running["device_num"] == 2, (
        f"自动分配的 device_num 应为 2，实际为 {running['device_num']!r}"
    )

    task = multi_card.wait_task(task_id)
    assert task["status"] == "completed", task.get("result")
    multi_card.wait_idle_at_least(baseline)


def test_multi_device_request_is_not_partially_allocated(multi_card):
    """45 · 多卡申请整单分配：缺一张就整单排队，空闲那张不许被预占。"""
    first, second = multi_card.require_idle(2)
    baseline = multi_card.idle_devices()

    blocker = multi_card.submit("sleep 20", device_ids=[first])
    multi_card.wait_task_running(blocker)
    multi_card.wait_idle_at_most(baseline - 1)

    task_id = multi_card.submit("sleep 2", device_ids=[first, second])
    for _ in range(4):
        entry = multi_card.queue_entry(task_id)
        assert entry is not None and entry["status"] == "queued", (
            f"卡 {first} 被别的任务占着，要 [{first}, {second}] 的任务 {task_id} "
            f"却{'不在队列里' if entry is None else '已经在 ' + entry['status']}"
            f"；多卡申请只能整单排队，不能部分分配"
        )
        assert second in multi_card.idle_minors(), (
            f"等 {first} 的期间卡 {second} 不被算作空闲了（{multi_card.idle_minors()}）"
            f"—— 拿不到的整单申请不能把这张卡先锁住"
        )
        time.sleep(multi_card.poll)

    # 占卡的走了 → 整单立刻能起，并且两张卡都真的归它。
    multi_card.client.delete_tasks([blocker])
    multi_card.wait_task(blocker)
    task = multi_card.wait_task(task_id)
    assert task["status"] == "completed", task.get("result")
    assert sorted(_devices_of(task)) == sorted([first, second]), task["devices"]
    multi_card.wait_idle_at_least(baseline)


def test_unsatisfiable_head_does_not_block_free_device(multi_card):
    """46 · 队首任务等的卡没空时，后面只要空闲那张卡的任务照跑。"""
    first, second = multi_card.require_idle(2)
    baseline = multi_card.idle_devices()

    blocker = multi_card.submit("sleep 20", device_ids=[first])
    multi_card.wait_task_running(blocker)

    # 高优先级那条要 first+second，而 first 被占 —— 它拿不到整单。
    head = multi_card.submit("sleep 1", device_ids=[first, second], priority=1)
    # 低优先级这条只要 second，而 second 是空的。
    tail = multi_card.submit("sleep 1", device_ids=[second], priority=0)

    tail_task = multi_card.wait_task(tail)
    assert tail_task["status"] == "completed", tail_task
    assert _devices_of(tail_task) == [second], tail_task["devices"]
    entry = multi_card.queue_entry(head)
    assert entry is not None and entry["status"] == "queued", (
        f"排在前面、要 [{first}, {second}] 的任务 {head} 应该还在排队，"
        f"实际 {entry}；first 仍然被 {blocker} 占着"
    )

    multi_card.client.delete_tasks([blocker])
    multi_card.wait_task(blocker)
    head_task = multi_card.wait_task(head)
    assert head_task["status"] == "completed", head_task.get("result")
    assert sorted(_devices_of(head_task)) == sorted([first, second]), head_task
    multi_card.wait_idle_at_least(baseline)


def test_multi_device_cancel_releases_all_devices(multi_card):
    """47 · 取消多卡任务后，两张卡都回空闲池。"""
    first, second = multi_card.require_idle(2)
    baseline = multi_card.idle_devices()

    task_id = multi_card.submit("sleep 300", device_ids=[first, second])
    running = multi_card.wait_task_running(task_id)
    assert sorted(_devices_of(running)) == sorted([first, second]), running["devices"]
    multi_card.wait_idle_at_most(baseline - 2)

    result = multi_card.client.delete_tasks([task_id])
    assert result.status == 200, result.text
    task = multi_card.wait_task(task_id)
    assert task["status"] == "cancelled", (
        f"取消后的多卡任务状态应为 cancelled（取消是独立终态，不再混在 failed "
        f"里，和 27 号同口径），实际 {task['status']}: {task}"
    )
    assert "手动取消" in (task["result"].get("error") or ""), (
        f"取消原因不明确: {task['result']}"
    )

    multi_card.wait_idle_at_least(baseline)
    idle = multi_card.idle_minors()
    assert first in idle and second in idle, (
        f"取消多卡任务后没有把卡都还回来：idle={idle}，期望含 [{first}, {second}]"
    )


def test_device_ids_override_device_num(multi_card):
    """48 · 非空 device_ids 优先于 device_num（文档契约）。"""
    device, = multi_card.require_idle(1)

    task_id = multi_card.submit("true", device_ids=[device], device_num=3)
    task = multi_card.wait_task(task_id)
    assert task["status"] == "completed", task.get("result")
    assert _devices_of(task) == [device], (
        f"给了 device_ids=[{device}] 还额外申请了 3 张，实际分到 {task['devices']}"
        f"；两者同时给时 device_ids 应该说了算"
    )
    assert task["device_num"] == 0, (
        f"device_ids 非空时 device_num 应为 0，实际 {task['device_num']!r}"
    )


def test_duplicate_device_ids_are_deduplicated(multi_card):
    """49 · device_ids 里的重复项被去重，不会变成"要两张卡"。"""
    device, = multi_card.require_idle(1)

    task_id = multi_card.submit("true", device_ids=[device, device])
    task = multi_card.wait_task(task_id)
    assert task["status"] == "completed", task.get("result")
    assert _devices_of(task) == [device], (
        f"device_ids=[{device}, {device}] 分到了 {task['devices']}；"
        f"同一张卡写两遍只该算一次申请（否则它会永远等不到资源）"
    )


def test_externally_busy_device_is_not_allocated(multi_card):
    """50 · 被别的作业占着的卡不会被发出去（排队而不是失败、更不是硬给）。

    ``npu_info.sh`` 把"有运行进程的 NPU"报成 busy，这是共享运行时（别人的
    vLLM 在跑）唯一能被 Worker 看见的外部占用来源。维护窗口里没有这种卡 ——
    那属于前置缺失，缺了直接失败并写清原因。
    """
    busy = multi_card.externally_busy_minors()
    if not busy:
        pytest.fail(
            "前置缺失：本机没有被外部作业占用的卡，这条用例无法验证。它要的是"
            "「npu_info.sh 报 busy 的卡不许被分配」——只在共享运行的机器上有这个"
            "场景；维护窗口全空闲时由 45/46（本地占用）覆盖同一段调度逻辑",
            pytrace=False,
        )

    minor = busy[0]
    task_id = multi_card.submit("true", device_ids=[minor])
    for _ in range(4):
        entry = multi_card.queue_entry(task_id)
        assert entry is not None and entry["status"] == "queued", (
            f"卡 {minor} 被外部作业占着（busy_ids={busy}），任务 {task_id} 却"
            f"{'不在队列里' if entry is None else '已经在 ' + entry['status']}"
            f"；外部占用的卡必须排队等，而不是发出去"
        )
        assert entry["devices"] == [], (
            f"排队中的任务已经拿到了设备 {entry['devices']}"
        )
        time.sleep(multi_card.poll)

    # 取消排队任务是"留痕"语义（记录保留、终态 cancelled、日志保留）——
    # 早先那版是"删记录 + 删日志"，已经统一掉了。
    deleted = multi_card.client.delete_tasks([task_id])
    assert deleted.status == 200, deleted.text
    cancelled = multi_card.get_task(task_id)
    assert cancelled["status"] == "cancelled", (
        f"排队任务 {task_id} 取消后状态应为 cancelled，实际 {cancelled['status']}"
    )
