"""统一取消：任务与 acquire 会话共用一套"判定 + 动作"。

两种条目的语义差异是刻意的：

  * 排队中的任务 → 摘出队列 + 记录留痕（终态 cancelled，日志保留）；
  * 运行中的任务 → 发取消信号，终态由执行体落成 cancelled；
  * 排队中的 acquire → 摘出队列 + 会话账本写 cancelled（没人再轮询，不留结果）；
  * 已经拿到卡的 acquire → 同一个调用里释放（= release 语义）。
"""

import threading
import types

import pytest

from neu_box.scheduling import resources
from neu_box.scheduling.queue import TaskQueue
from neu_box.scheduling.sessions import AcquireFailure, AcquireRequest
from neu_box.runtime.sandbox import SbxManager


class _Db:
    def __init__(self):
        self.task_results = []
        self.session_states = []
        self.deleted = []

    def update_task_result(self, task_id, status, *args, **kwargs):
        self.task_results.append((task_id, status))

    def update_session_state(self, request_id, state, **kwargs):
        self.session_states.append((request_id, state, kwargs.get('code')))

    def delete_task(self, task_id):
        self.deleted.append(task_id)

    def cleanup_old_sessions(self, keep=200):
        return 0


class _FakeSbx:
    def __init__(self):
        self.evacuated = []

    def evacuate_caller(self, sandbox_name, pid):
        self.evacuated.append((sandbox_name, pid))
        return True


def _queue():
    queue = TaskQueue.__new__(TaskQueue)
    queue._lock = threading.RLock()
    queue._round_lock = threading.Lock()
    queue._dispatching = 0
    queue._queues = {1: {}, 0: {}}
    queue._running = {}
    queue._running_acquires = {}
    queue._acquire_results = {}
    queue._maintenance_errors = {}
    queue._loop = None
    queue._wake = None
    queue._db = _Db()
    queue._remove_log = lambda task_id: None
    return queue


def _acquire(request_id='req1'):
    return AcquireRequest(
        request_id=request_id, owner='root', pid=1234, cpu=0, mem='0',
        device_num=1, device_ids=['0'], device_root=None, validator=None,
        created_at=1.0, priority=0,
    )


def test_cancel_queued_task_keeps_the_record():
    queue = _queue()
    queue._enqueue('task', 't1', {'task_id': 't1', 'priority': 0})

    outcome = queue.cancel('task', 't1')

    assert outcome['status'] == 'cancelled'
    assert queue._lookup('task', 't1') is None
    assert queue._db.task_results == [('t1', 'cancelled')], (
        '排队任务取消应当留痕（终态 cancelled），而不是删记录'
    )
    assert queue._db.deleted == []


def test_cancel_running_task_signals_the_executor():
    queue = _queue()

    class Executor:
        def __init__(self):
            self.canceled = False

        def cancel(self):
            self.canceled = True

    executor = Executor()
    task = {'task_id': 't2', '_executor': executor}
    queue._running['t2'] = task

    outcome = queue.cancel('task', 't2')

    assert outcome['status'] == 'cancelling'
    assert task['_canceled'] is True
    assert executor.canceled is True


def test_cancel_terminal_task_reports_terminal():
    queue = _queue()
    assert queue.cancel('task', 'nope')['status'] == 'terminal'


def test_cancel_pending_acquire_dequeues_and_records_state():
    queue = _queue()
    request = _acquire()
    queue._enqueue('acquire', request.request_id, request, 0)

    outcome = queue.cancel('acquire', request.request_id)

    assert outcome['status'] == 'cancelled'
    assert queue._lookup('acquire', request.request_id) is None
    assert queue._db.session_states == [
        (request.request_id, 'cancelled', 'client_cancelled')
    ], '排队中的 acquire 取消要写账本，但不需要给轮询留结果'
    assert request.request_id not in queue._acquire_results


def test_cancel_active_acquire_releases_it_in_place(monkeypatch):
    queue = _queue()
    sbx = _FakeSbx()
    monkeypatch.setattr(SbxManager, 'get_instance', staticmethod(lambda: sbx))
    released = []
    queue._running_acquires['sbx_root_1.slice'] = {
        'request_id': 'req9', 'owner': 'root', 'pid': 1,
        'sandbox_name': 'sbx_root_1.slice', 'devices': ['234:0'],
        'created_at': 1.0, 'acquired_at': 2.0,
    }
    queue._release_acquire_locked = lambda name: released.append(name) or True

    outcome = queue.cancel('acquire', 'req9', host_pid=222)

    assert outcome['status'] == 'released'
    assert outcome['sandbox_name'] == 'sbx_root_1.slice'
    assert released == ['sbx_root_1.slice'], '拿到卡之后取消 = 在同一个调用里 release'
    assert sbx.evacuated == [('sbx_root_1.slice', 222)], (
        '释放前必须先把调用方搬出沙盒 cgroup，否则它会杀掉自己'
    )


def test_cancel_unknown_acquire_is_unknown_and_idempotent():
    queue = _queue()
    request = _acquire('req2')
    queue._enqueue('acquire', 'req2', request, 0)

    assert queue.cancel('acquire', 'req2')['status'] == 'cancelled'
    assert queue.cancel('acquire', 'req2')['status'] == 'unknown'


def test_cancel_rejects_unknown_kind():
    queue = _queue()
    with pytest.raises(ValueError):
        queue.cancel('bogus', 'x')


def test_cancel_release_failure_is_reported(monkeypatch):
    queue = _queue()
    monkeypatch.setattr(SbxManager, 'get_instance', staticmethod(lambda: _FakeSbx()))
    queue._running_acquires['sbx_a.slice'] = {
        'request_id': 'req3', 'owner': 'root', 'pid': 1,
        'sandbox_name': 'sbx_a.slice', 'devices': [], 'created_at': 1.0,
        'acquired_at': 2.0,
    }
    queue._release_acquire_locked = lambda name: False

    assert queue.cancel('acquire', 'req3')['status'] == 'release_failed'


def test_delete_tasks_deletes_only_terminal_records():
    queue = _queue()
    queue._enqueue('task', 'queued', {'task_id': 'queued', 'priority': 0})

    assert queue.delete_tasks(['queued', 'gone']) == 2
    assert queue._db.task_results == [('queued', 'cancelled')]
    assert queue._db.deleted == ['gone'], '终态/不存在的老路径仍然是"真删"'
