"""第 3 层 · 多卡（manifest 30-31）。

组夹具 ``multi_card`` 已经确认至少有两张空闲设备；这里不再判断卡数。
"""

from __future__ import annotations

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

    first_task = multi_card.submit("sleep 15", device_ids=[first])
    second_task = multi_card.submit("sleep 15", device_ids=[second])

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

    task_id = multi_card.submit("sleep 6", device_ids=[first, second])
    running = multi_card.wait_task_running(task_id)
    devices = _devices_of(running)
    assert devices == [first, second], (
        f"多卡任务应同时持有 {first}/{second}，实际为 {devices}"
    )
    assert running["device_num"] == 2, (
        f"多卡任务的 device_num 应为 2，实际为 {running['device_num']!r}"
    )
    multi_card.wait_idle_at_most(baseline - 2)

    task = multi_card.wait_task(task_id)
    assert task["status"] == "completed", task.get("result")
    multi_card.wait_idle_at_least(baseline)
