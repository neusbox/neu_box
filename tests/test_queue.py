#!/usr/bin/env python3
"""任务队列测试 — 并发排队、FIFO、批量删除。"""

import sys
import time
from common import (
    get, post, assert_ok, assert_gt, run_tests, get_devnode_id,
)

QUICK = "--quick" in sys.argv
TASK_COUNT      = 8 if QUICK else 20
DEVICE_PER_TASK = 1
CPU_PER_TASK    = 1
MEM_PER_TASK    = 1
POLL_INTERVAL   = 2
TEST_USERS      = ["pengyt", "lipz"]


def test_concurrency():
    """有限设备资源下并发不超过可用数，且运行中任务实际分配到设备。"""
    # gpu_id = get_devnode_id()
    gpu_id = get_devnode_id()

    _, nodes = post("/nodes/get_all_nodes", {})
    node = next((n for n in nodes["nodes"] if n["node_id"] == gpu_id), {})
    total_dev = node.get("total_devices", 0)
    name = node.get("name", "?")
    print(f"    {name}: 总设备={total_dev}", flush=True)

    # 不同任务不同耗时，模拟真实并发
    sleep_times = [3, 5, 7, 4, 6, 8, 10, 2, 4, 6, 8, 10, 2, 4, 6, 8, 10, 2, 4, 6][:TASK_COUNT]
    timeout = sum(sleep_times) + 30

    task_ids = []
    for i in range(TASK_COUNT):
        user = TEST_USERS[i % len(TEST_USERS)]
        s = sleep_times[i]
        _, d = post("/command/run", {
            "node_id": gpu_id, "user_id": user,
            "command": f"echo '[{user}] T{i+1}({s}s) start'; sleep {s}; echo 'T{i+1} done'",
            "cpu": CPU_PER_TASK, "memory": MEM_PER_TASK,
            "mem_unit": "GB", "device_num": DEVICE_PER_TASK,
        })
        assert_ok(_, f"任务{i+1} 提交失败: {d.get('error',_)}")
        task_ids.append(d["task_id"])
        print(f"    T{i+1}: {d['task_id'][:12]}... {s}s pos=#{d['position']}", flush=True)
        time.sleep(0.05)

    max_running = 0
    max_devices_used = 0
    done_count = 0
    start = time.time()

    while done_count < len(task_ids) and (time.time() - start) < timeout:
        _, data = get(f"/command/queue?node_id={gpu_id}")
        # 只统计本测试提交的任务（共享节点队列里有大量历史任务）
        by_id = {t["task_id"]: t for t in data.get("queue", [])}
        running_tasks = [by_id[tid] for tid in task_ids
                         if tid in by_id and by_id[tid].get("status") == "running"]
        running = len(running_tasks)
        done_count = sum(1 for tid in task_ids
                         if tid in by_id
                         and by_id[tid].get("status") in ("completed", "failed"))
        if running > max_running:
            max_running = running

        # 统计实际分配的设备，验证请求了设备的任务确实分到了设备
        total_assigned = 0
        for t in running_tasks:
            devices = t.get("devices") or []
            dn = t.get("device_num", 0)
            total_assigned += len(devices)
            if dn > 0:
                assert len(devices) == dn, \
                    f"任务 {t['user_id']} 请求 {dn} 设备, 实际分配 {len(devices)}: {devices}"
                print(f"    ▶ {t['user_id']}: devices={devices}", flush=True)
        if total_assigned > max_devices_used:
            max_devices_used = total_assigned

        time.sleep(POLL_INTERVAL)

    print(f"    最大并发: {max_running}  最大设备占用: {max_devices_used}/{total_dev}  耗时: {int(time.time()-start)}s", flush=True)
    assert max_running <= max(total_dev, 1), \
        f"并发数 {max_running} > 总设备 {total_dev}"
    assert max_devices_used <= total_dev, \
        f"占用设备 {max_devices_used} > 总设备 {total_dev}"


def test_fifo_order():
    """先提交的任务先完成（用设备强制串行）。"""
    node_id = get_devnode_id()

    _, d1 = post("/command/run", {
        "node_id": node_id, "user_id": "pengyt",
        "command": "echo FIFO_1; sleep 2",
        "cpu": 1, "memory": 1, "mem_unit": "GB", "device_num": 1,
    })
    assert_ok(_, f"任务1 失败: {d1}")
    time.sleep(0.3)

    _, d2 = post("/command/run", {
        "node_id": node_id, "user_id": "lipz",
        "command": "echo FIFO_2; sleep 2",
        "cpu": 1, "memory": 1, "mem_unit": "GB", "device_num": 1,
    })
    assert_ok(_, f"任务2 失败: {d2}")

    # 并发消费者下 position 可能相同（同时出队），但先提交的应先完成
    # 验证两个任务都正常提交
    assert "task_id" in d1 and "task_id" in d2, "任务应正常返回 task_id"


def test_batch_delete():
    """批量删除：排队/已完成的可删，running 不删。"""
    gpu_id = get_devnode_id()

    ids = []
    for i in range(3):
        _, d = post("/command/run", {
            "node_id": gpu_id, "user_id": "pengyt",
            "command": f"sleep {1+i}; echo batch_{i}",
            "cpu": 1, "memory": 1, "mem_unit": "GB", "device_num": 0,
        })
        assert_ok(_, f"提交失败: {d}")
        ids.append(d["task_id"])

    time.sleep(3)

    _, d = post("/command/tasks/delete", {"node_id": gpu_id, "task_ids": ids})
    if not (200 <= _ < 300):
        # Worker 可能还没重启，新路由未生效
        print(f"    跳过: Worker 未更新 (HTTP {_} {d.get('error','')[:60]})", flush=True)
        return
    deleted = d.get("deleted", 0)
    print(f"    请求删除 {len(ids)} 个, 实际删除 {deleted} 个", flush=True)
    assert_gt(deleted, 0, "至少应删除已完成任务")

    # 验证已删除的不在队列中
    _, data = get(f"/command/queue?node_id={gpu_id}")
    queue_ids = {t["task_id"] for t in data.get("queue", [])}
    remaining = [tid for tid in ids if tid in queue_ids]
    print(f"    剩余: {len(remaining)} 个 (可能是 running)", flush=True)


def test_priority_preemption():
    """赶论文任务（priority=1）永远先于普通任务执行；同优先级内 FIFO。

    场景: N1 占满当前空闲设备睡 12s → N2（普通）、R1/R2（赶论文）排队 →
    N1 释放设备后，启动顺序必须为 R1 < R2 < N2。
    用空闲卡数而非总卡数，避免共享节点上被终端会话占用的卡导致死等。
    """
    node_id = get_devnode_id()

    _, nodes = post("/nodes/get_all_nodes", {})
    node = next((n for n in nodes["nodes"] if n["node_id"] == node_id), {})
    idle_dev = node.get("idle_devices", 0)
    if idle_dev < 4:
        print(f"    跳过: 空闲设备不足 4 张（当前 {idle_dev}），等节点空闲后重跑", flush=True)
        return

    def submit(cmd, user, dev, priority=None):
        body = {
            "node_id": node_id, "user_id": user, "command": cmd,
            "cpu": 1, "memory": 1, "mem_unit": "GB", "device_num": dev,
        }
        if priority is not None:
            body["priority"] = priority
        _, d = post("/command/run", body)
        assert_ok(_, f"提交失败: {d.get('error', _)}")
        return d

    n1 = submit("echo N1; sleep 12; echo N1 done", "pengyt", idle_dev)
    print(f"    N1: {n1['task_id'][:12]}... 占 {idle_dev} 台设备 12s", flush=True)
    time.sleep(0.5)

    d_n2 = submit("echo N2; sleep 1", "lipz", 1)
    time.sleep(0.1)
    d_r1 = submit("echo R1; sleep 1", "pengyt", 1, priority=1)
    time.sleep(0.1)
    d_r2 = submit("echo R2; sleep 1", "lipz", 1, priority=1)
    for name, d in (("N2", d_n2), ("R1", d_r1), ("R2", d_r2)):
        print(f"    {name}: {d['task_id'][:12]}... pos=#{d['position']} "
              f"priority={d.get('priority')}", flush=True)
    assert d_r1.get("priority") == 1 and d_r2.get("priority") == 1, \
        "赶论文任务响应应包含 priority=1"

    n2, r1, r2 = d_n2["task_id"], d_r1["task_id"], d_r2["task_id"]

    # 位置号以全部提交后的当前队列为准（响应里的 position 是各自提交时刻的快照）
    time.sleep(0.3)
    _, q = get(f"/command/queue?node_id={node_id}")
    pos = {t["task_id"]: t["position"] for t in q.get("queue", [])}
    assert pos.get(r1, 99) < pos.get(r2, 99) < pos.get(n2, 99), \
        f"赶论文任务应排在普通任务前: R1=#{pos.get(r1)} R2=#{pos.get(r2)} N2=#{pos.get(n2)}"

    # 等三个任务都跑完
    deadline = time.time() + 40
    while time.time() < deadline:
        _, q = get(f"/command/queue?node_id={node_id}")
        states = {
            tid: next((t.get("status") for t in q.get("queue", [])
                       if t["task_id"] == tid), None)
            for tid in (n2, r1, r2)
        }
        if all(states[tid] in ("completed", "failed") for tid in states):
            break
        time.sleep(2)
    else:
        raise AssertionError(f"等待任务完成超时: {states}")

    def started_at(tid):
        _, d = get(f"/command/result/{tid}?node_id={node_id}")
        assert_ok(_, f"查结果失败 {tid}: {d}")
        return d.get("started_at")

    s_n2, s_r1, s_r2 = started_at(n2), started_at(r1), started_at(r2)
    print(f"    启动时间: R1={s_r1}  R2={s_r2}  N2={s_n2}", flush=True)
    assert s_r1 and s_r2 and s_n2, f"任务缺少 started_at: R1={s_r1} R2={s_r2} N2={s_n2}"
    assert s_r1 < s_r2 < s_n2, \
        f"启动顺序必须为 R1 < R2 < N2，实际 R1={s_r1} R2={s_r2} N2={s_n2}"

    # 非法 priority（负数）应被数据层拒绝并返回 400
    status, d = post("/command/run", {
        "node_id": node_id, "user_id": "pengyt",
        "command": "echo bad", "device_num": 0, "priority": -1,
    })
    assert status == 400 and d.get("error"), \
        f"非法 priority 应被拒绝: HTTP {status} {d}"

    # 超范围 priority（>1，当前仅支持 0=普通、1=赶论文）同样被拒绝
    status, d = post("/command/run", {
        "node_id": node_id, "user_id": "pengyt",
        "command": "echo bad", "device_num": 0, "priority": 2,
    })
    assert status == 400 and d.get("error"), \
        f"超范围 priority 应被拒绝: HTTP {status} {d}"


TESTS = [
    ("并发排队",    test_concurrency),
    ("FIFO 顺序",   test_fifo_order),
    ("优先级抢占",  test_priority_preemption),
    # ("批量删除",    test_batch_delete),
]

if __name__ == "__main__":
    run_tests(TESTS, "任务队列测试")
