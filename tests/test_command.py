#!/usr/bin/env python3
"""命令任务测试 — 提交验证、结果详情、错误处理。"""

import time
from common import (
    delete, get, post, assert_ok, assert_eq, assert_gt, assert_in, run_tests,
    get_gpunode_id,
)


def test_submit_validation():
    """缺少必填字段应返回错误。"""
    gpu_id = get_gpunode_id()

    # 缺 node_id
    s, d = post("/tasks", {"user_id": "test", "command": "echo hi"})
    assert not (200 <= s < 300), "缺少 node_id 应该失败"
    assert "error" in d

    # 缺 user_id
    s, d = post("/tasks", {"node_id": gpu_id, "command": "echo hi"})
    assert not (200 <= s < 300), "缺少 user_id 应该失败"

    # 缺 command
    s, d = post("/tasks", {"node_id": gpu_id, "user_id": "test"})
    assert not (200 <= s < 300), "缺少 command 应该失败"

    # 正常
    s, d = post("/tasks", {
        "node_id": gpu_id, "user_id": "pengyt",
        "command": "echo ok", "cpu": 1, "memory": 1,
        "mem_unit": "GB", "device_num": 0,
    })
    assert_ok(s, f"正常提交应成功: {d}")
    assert "task_id" in d
    print(f"    3 项校验通过", flush=True)


def test_result_stdout_stderr():
    """验证结果包含 stdout 和 stderr。"""
    gpu_id = get_gpunode_id()
    marker = f"MARK_{int(time.time())}"

    _, d = post("/tasks", {
        "node_id": gpu_id, "user_id": "pengyt",
        "command": f"echo OUT:{marker}; echo ERR:{marker} >&2",
        "cpu": 1, "memory": 1, "mem_unit": "GB", "device_num": 0,
    })
    assert_ok(_, f"提交失败: {d}")
    task_id = d["task_id"]

    # 等待完成（最长等 60s，每秒查一次）
    for _ in range(60):
        _, data = get(f"/tasks/{task_id}?node_id={gpu_id}")
        if data.get("status") in ("completed", "failed"):
            break
        time.sleep(1)

    assert_eq(data.get("status"), "completed", f"任务未完成 (pos={data.get('position','?')}): {data.get('command','')[:50]}")
    r = data.get("result", {})
    assert_in(marker, r.get("stdout", ""), "stdout 缺少 marker")
    assert_in(marker, r.get("stderr", ""), "stderr 缺少 marker")
    print(f"    stdout={len(r['stdout'])}B stderr={len(r['stderr'])}B", flush=True)


def test_resource_configs():
    """不同资源组合 (0资源 / CPU+Mem / 1GPU) 都能正常完成。"""
    gpu_id = get_gpunode_id()
    configs = [
        {"cpu": 0, "memory": 0, "device_num": 0, "label": "零资源"},
        {"cpu": 4, "memory": 4, "device_num": 0, "label": "仅CPU+Mem"},
        {"cpu": 2, "memory": 2, "device_num": 1, "label": "1GPU"},
    ]

    ids = []
    for cfg in configs:
        _, d = post("/tasks", {
            "node_id": gpu_id, "user_id": "pengyt",
            "command": f"echo '{cfg['label']} OK'; sleep 2",
            "cpu": cfg["cpu"], "memory": cfg["memory"],
            "mem_unit": "GB", "device_num": cfg["device_num"],
        })
        assert_ok(_, f"{cfg['label']} 提交失败: {d}")
        ids.append(d["task_id"])
        print(f"    {cfg['label']}: pos=#{d['position']}", flush=True)

    time.sleep(5)
    if ids:
        delete("/tasks", {"node_id": gpu_id, "task_ids": ids})
    print(f"    {len(configs)} 种配置完成", flush=True)


TESTS = [
    ("提交验证",        test_submit_validation),
    ("结果 stdout/stderr", test_result_stdout_stderr),
    ("多资源配置",      test_resource_configs),
]

if __name__ == "__main__":
    run_tests(TESTS, "命令任务测试")
