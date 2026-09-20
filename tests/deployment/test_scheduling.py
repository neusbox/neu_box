"""第 3 层 · 调度（manifest 52-53）。

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
