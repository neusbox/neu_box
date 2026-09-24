"""release 时"搬走调用方"的判定（``SbxManager.evacuate_caller``）。

背景：`neubox release` 是被借出去的 shell fork 出来的子进程 —— cgroup 成员身份
随 fork 继承，所以它在沙盒 cgroup 里，却没有 origin。销毁的最后一步是
``cgroup.kill``，于是它会把自己一起杀掉（用户看到 ``zsh: killed``）。

这里只锁"搬不搬、搬到哪"的判定；真正的 cgroup 迁移由部署验收
（``tests/deployment``）在真机上验。
"""

from neu_box.runtime.sandbox import SbxManager


class _Db:
    def __init__(self, record):
        self._record = record

    def get_sandbox(self, name):
        return self._record


def _manager(record, pids, moved=None, parent=0):
    """搭一个只带"读快照 / 搬进程"两个替身的 manager。"""
    manager = SbxManager.__new__(SbxManager)
    manager.db = _Db(record)
    manager._read_cgroup_snapshot = lambda name: (list(pids), True)
    manager._parent_pid = staticmethod(lambda pid: parent)
    if moved is None:
        moved = []
    manager.move_pid_to_cgroup = lambda pid, path: moved.append((pid, path)) or True
    return manager, moved


def test_caller_moves_to_its_parent_origin():
    """neubox 的情形：调用方是借来的 shell 的子进程 → 搬回 shell 的 origin。"""
    record = {'origins': {'111': '/user.slice/session-1.scope'}, 'pids': [222]}
    manager, moved = _manager(record, pids=[111, 222, 333], parent=111)

    assert manager.evacuate_caller('sbx_a.slice', 222) is True
    assert moved == [(222, '/user.slice/session-1.scope')]


def test_caller_with_its_own_origin_moves_to_that_origin():
    record = {'origins': {'222': '/user.slice/own.scope'}, 'pids': [222]}
    manager, moved = _manager(record, pids=[222], parent=0)

    assert manager.evacuate_caller('sbx_a.slice', 222) is True
    assert moved == [(222, '/user.slice/own.scope')]


def test_caller_not_in_this_sandbox_is_left_alone():
    """不在这个沙盒 cgroup 里的 PID 一律不动 —— 否则就是个"搬任意 PID"的接口。"""
    record = {'origins': {'111': '/user.slice/session-1.scope'}, 'pids': [111]}
    manager, moved = _manager(record, pids=[111], parent=111)

    assert manager.evacuate_caller('sbx_a.slice', 999) is False
    assert moved == []


def test_caller_without_any_origin_is_left_alone():
    """父进程也没有 origin（沙盒里长出来的子树）→ 不搬，保持 fail-closed。"""
    record = {'origins': {'111': '/user.slice/session-1.scope'}, 'pids': [111]}
    manager, moved = _manager(record, pids=[111, 222], parent=777)

    assert manager.evacuate_caller('sbx_a.slice', 222) is False
    assert moved == []


def test_task_sandbox_without_origins_is_never_touched():
    """命令任务的沙盒没有 origin 表 → 没有任何"家"可回，一律不动。"""
    record = {'origins': {}, 'pids': [222]}
    manager, moved = _manager(record, pids=[222], parent=111)

    assert manager.evacuate_caller('sbx_user_task.slice', 222) is False
    assert moved == []


def test_bogus_pid_arguments_are_rejected():
    record = {'origins': {'111': '/user.slice/session-1.scope'}, 'pids': [111]}
    manager, moved = _manager(record, pids=[111, 222, 333], parent=111)

    for value in ('abc', None, 0, 1, -5):
        assert manager.evacuate_caller('sbx_a.slice', value) is False
    assert moved == []


def test_failed_move_is_reported_not_swallowed():
    record = {'origins': {'111': '/user.slice/session-1.scope'}, 'pids': [111]}
    manager = SbxManager.__new__(SbxManager)
    manager.db = _Db(record)
    manager._read_cgroup_snapshot = lambda name: ([111, 222], True)
    manager._parent_pid = staticmethod(lambda pid: 111)
    manager.move_pid_to_cgroup = lambda pid, path: False

    assert manager.evacuate_caller('sbx_a.slice', 222) is False


def test_parent_pid_reads_proc_stat_with_spaces_in_comm():
    """comm 带空格/括号时也要取到正确的 PPID（真实 /proc 读法）。"""
    import os

    assert SbxManager._parent_pid(os.getpid()) == os.getppid()
