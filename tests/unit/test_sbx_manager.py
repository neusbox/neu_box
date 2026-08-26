"""SbxManager 外部设备占用查询的 fail-closed 行为测试。

设备状态脚本（npu-smi/gpu-info 封装）在高负载下可能超时或返回 total=0。
若此时返回空集，所有外部占用的卡会被误判为空闲并重新分配（2026-08-24
事故：fork 风暴期间 npu-smi 卡死 + vLLM 部署抖动，任务被分配到外部
正在使用的卡上）。修复：查询失败时沿用上一次成功结果。
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from neu_box.worker.executor import sbx_manager as sm  # noqa: E402


class FakeMgmt(sm.SbxManager):
    """绕过 __init__（避免真实 DB / cgroup 恢复）的测试替身。"""

    def __init__(self):
        self._last_external_busy = set()

    def _discover_device_nodes(self):
        return ["235:0", "235:1", "235:2", "235:3"]

    def _get_allocated_devices(self):
        return set()


@pytest.fixture
def fake_script(monkeypatch):
    """把设备状态脚本调用替换为可编排的假实现。"""
    state = {"payload": '{"total":4,"idle":4,"busy_ids":[]}', "exc": None}

    def _check_output(cmd, **kw):
        if state["exc"] is not None:
            raise state["exc"]
        return state["payload"].encode()

    monkeypatch.setattr(sm.subprocess, "check_output", _check_output)
    monkeypatch.setattr(sm, "env_text",
                        lambda name, legacy=None: "/fake/npu_info.sh")
    return state


def query(state, mgr=None):
    mgr = mgr or FakeMgmt()
    return sm.SbxManager._get_external_busy_devices(mgr)


def test_normal_query(fake_script):
    fake_script["payload"] = '{"total":4,"idle":2,"busy_ids":[2,3]}'
    assert query(fake_script) == {"235:2", "235:3"}


def test_timeout_keeps_last_known_busy(fake_script):
    mgr = FakeMgmt()
    fake_script["payload"] = '{"total":4,"idle":2,"busy_ids":[2,3]}'
    query(fake_script, mgr)
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    assert query(fake_script, mgr) == {"235:2", "235:3"}


def test_total_zero_is_treated_as_failure(fake_script):
    mgr = FakeMgmt()
    fake_script["payload"] = '{"total":4,"idle":2,"busy_ids":[2,3]}'
    query(fake_script, mgr)
    # npu-smi 不可用时脚本输出 total=0，不能当作"全部空闲"
    fake_script["payload"] = '{"total":0,"idle":0,"busy_ids":[]}'
    assert query(fake_script, mgr) == {"235:2", "235:3"}


def test_recovery_refreshes_and_failure_uses_newest(fake_script):
    mgr = FakeMgmt()
    fake_script["payload"] = '{"total":4,"idle":2,"busy_ids":[2,3]}'
    query(fake_script, mgr)
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    query(fake_script, mgr)
    # 恢复后刷新
    fake_script["exc"] = None
    fake_script["payload"] = '{"total":4,"idle":3,"busy_ids":[1]}'
    assert query(fake_script, mgr) == {"235:1"}
    # 之后的失败沿用最新值 {1}，而不是更早的 {2,3}
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    assert query(fake_script, mgr) == {"235:1"}


def test_first_failure_without_history_returns_empty(fake_script):
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    assert query(fake_script) == set()


def test_free_devices_excludes_sticky_busy_on_failure(fake_script):
    """端到端：脚本失败时 _get_free_devices 仍排除最后已知忙碌设备。"""
    mgr = FakeMgmt()
    fake_script["payload"] = '{"total":4,"idle":2,"busy_ids":[2,3]}'
    assert sm.SbxManager._get_free_devices(mgr) == ["235:0", "235:1"]
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    assert sm.SbxManager._get_free_devices(mgr) == ["235:0", "235:1"]
