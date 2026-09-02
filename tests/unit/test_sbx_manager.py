"""SbxManager 外部设备占用查询的 fail-closed 行为测试。

设备状态脚本（npu-smi/gpu-info 封装）在高负载下可能超时或返回 total=0。
若此时返回空集，所有外部占用的卡会被误判为空闲并重新分配（2026-08-24
事故：fork 风暴期间 npu-smi 卡死 + vLLM 部署抖动，任务被分配到外部
正在使用的卡上）。修复：查询失败时沿用上一次成功结果；若启动后尚无
成功结果，则把全部受管设备视为忙碌。
"""
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from neu_box.worker.executor import sbx_manager as sm  # noqa: E402


class FakeMgmt(sm.SbxManager):
    """绕过 __init__（避免真实 DB / cgroup 恢复）的测试替身。"""

    def __init__(self):
        self._last_external_busy = None

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
                        lambda name: "/fake/npu_info.sh")
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


def test_first_failure_without_history_marks_all_managed_busy(fake_script):
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    assert query(fake_script) == {"235:0", "235:1", "235:2", "235:3"}


def test_first_failure_without_history_leaves_no_free_devices(fake_script):
    mgr = FakeMgmt()
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    assert sm.SbxManager._get_free_devices(mgr) == []


def test_free_devices_excludes_sticky_busy_on_failure(fake_script):
    """端到端：脚本失败时 _get_free_devices 仍排除最后已知忙碌设备。"""
    mgr = FakeMgmt()
    fake_script["payload"] = '{"total":4,"idle":2,"busy_ids":[2,3]}'
    assert sm.SbxManager._get_free_devices(mgr) == ["235:0", "235:1"]
    fake_script["exc"] = subprocess.TimeoutExpired("npu-smi", 10)
    assert sm.SbxManager._get_free_devices(mgr) == ["235:0", "235:1"]


class _ReaperDB:
    def __init__(self, pids=None):
        self.records = {
            'sbx_user_task.slice': {
                'name': 'sbx_user_task.slice',
                'pids': list(pids or []),
                'devices': ['235:0'],
                'created_at': time.time() - 3600,
            },
        }
        self.pid_updates = []

    def list_sandboxes(self):
        return [dict(record) for record in self.records.values()]

    def get_sandbox(self, name):
        record = self.records.get(name)
        return dict(record) if record else None

    def update_sandbox_pids(self, name, pids):
        self.records[name]['pids'] = list(pids)
        self.pid_updates.append((name, list(pids)))

    def delete_sandbox(self, name):
        self.records.pop(name, None)


class _ReaperManager(sm.SbxManager):
    def __init__(self, snapshots, pids=None, destroy_results=None):
        self.db = _ReaperDB(pids)
        self._lock = threading.RLock()
        self.snapshots = list(snapshots)
        self.last_snapshot = None
        self.destroy_results = list(destroy_results or [True])
        self.destroy_attempts = []

    def _read_cgroup_snapshot(self, _name):
        if self.snapshots:
            value = self.snapshots.pop(0)
            if not isinstance(value, BaseException):
                self.last_snapshot = value
        else:
            value = self.last_snapshot
        if isinstance(value, BaseException):
            raise value
        return value

    def destroy_sandbox(self, name):
        self.destroy_attempts.append(name)
        result = self.destroy_results.pop(0) if self.destroy_results else True
        if result:
            self.db.delete_sandbox(name)
        return result

    def list_sandboxes_via_cli(self):
        return []


def test_reaper_ignores_reused_historical_pid_and_reclaims_device(monkeypatch):
    """历史 PID 即使被复用且存活，也不能阻止空 cgroup 释放卡。"""
    manager = _ReaperManager(
        snapshots=[([], False), ([], False)],
        pids=[4242],
    )

    def forbidden_kill(*_args):
        raise AssertionError('Reaper 不应再用 kill(pid, 0) 判断 cgroup 归属')

    monkeypatch.setattr(sm.os, 'kill', forbidden_kill)
    assert manager.cleanup_orphaned() == 1
    assert manager.destroy_attempts == ['sbx_user_task.slice']
    assert manager.db.records == {}


def test_reaper_keeps_populated_hierarchy_even_when_pid_snapshot_is_empty():
    """populated 会递归覆盖子 cgroup，不能因一次 PID 扫描为空误收尸。"""
    manager = _ReaperManager(
        snapshots=[([], True)],
        pids=[111],
    )
    assert manager.cleanup_orphaned() == 0
    assert manager.destroy_attempts == []
    assert manager.db.records['sbx_user_task.slice']['pids'] == []


def test_reaper_retries_read_failure_then_reclaims_empty_sandbox():
    manager = _ReaperManager(
        snapshots=[
            OSError('temporary cgroup read failure'),
            ([], False),
            ([], False),
        ],
        pids=[111],
    )
    assert manager.cleanup_orphaned() == 0
    assert manager.db.records
    assert manager.cleanup_orphaned() == 1
    assert manager.db.records == {}


def test_reaper_retries_failed_destroy_until_ebpf_cleanup_succeeds():
    manager = _ReaperManager(
        snapshots=[([], False), ([], False)],
        pids=[111],
        destroy_results=[False, True],
    )
    assert manager.cleanup_orphaned() == 0
    assert manager.db.records
    assert manager.cleanup_orphaned() == 1
    assert manager.destroy_attempts == [
        'sbx_user_task.slice',
        'sbx_user_task.slice',
    ]


def test_reaper_missing_cgroup_still_runs_destroy_for_ebpf_cleanup():
    manager = _ReaperManager(
        snapshots=[None],
        pids=[111],
    )
    assert manager.cleanup_orphaned() == 1
    assert manager.destroy_attempts == ['sbx_user_task.slice']


def test_cgroup_snapshot_recursively_reads_child_processes(tmp_path):
    cgroup = tmp_path / 'sandbox_sbx_user_task.slice'
    child = cgroup / 'container-child'
    child.mkdir(parents=True)
    (cgroup / 'cgroup.procs').write_text('101\n', encoding='utf-8')
    (child / 'cgroup.procs').write_text('202\n', encoding='utf-8')
    (cgroup / 'cgroup.events').write_text(
        'populated 1\nfrozen 0\n', encoding='utf-8'
    )
    manager = object.__new__(sm.SbxManager)
    manager._cg_path = lambda _name: str(cgroup)

    assert manager._read_cgroup_snapshot('sbx_user_task.slice') == (
        [101, 202],
        True,
    )


def test_cgroup_snapshot_populated_zero_discards_exited_pid(tmp_path):
    cgroup = tmp_path / 'sandbox_sbx_user_task.slice'
    cgroup.mkdir()
    (cgroup / 'cgroup.procs').write_text('303\n', encoding='utf-8')
    (cgroup / 'cgroup.events').write_text('populated 0\n', encoding='utf-8')
    manager = object.__new__(sm.SbxManager)
    manager._cg_path = lambda _name: str(cgroup)

    assert manager._read_cgroup_snapshot('sbx_user_task.slice') == (
        [],
        False,
    )


def test_join_replaces_db_pid_history_with_cgroup_snapshot():
    manager = object.__new__(sm.SbxManager)
    manager.db = _ReaperDB(pids=[10, 11, 12])
    manager._lock = threading.RLock()
    manager._run_cli = lambda *_args: SimpleNamespace(
        returncode=0,
        stdout='',
        stderr='',
    )
    manager._read_cgroup_snapshot = lambda _name: ([22, 23], True)

    assert manager.join_sandbox('sbx_user_task.slice', 22)
    assert manager.db.records['sbx_user_task.slice']['pids'] == [22, 23]


def test_destroy_timeout_keeps_record_for_next_reaper_scan(tmp_path):
    manager = object.__new__(sm.SbxManager)
    manager.db = _ReaperDB(pids=[])
    manager._lock = threading.RLock()
    manager._cg_path = lambda _name: str(tmp_path / 'missing-cgroup')

    def timeout(*_args):
        raise subprocess.TimeoutExpired('neu-box-sandbox destroy', 30)

    manager._run_cli = timeout
    assert not manager.destroy_sandbox('sbx_user_task.slice')
    assert 'sbx_user_task.slice' in manager.db.records
