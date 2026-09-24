"""调度回合锁：一回合内队列不被修改。

这是"没有中间派发状态"的来源 —— 有了它，`_dispatch` 不需要"分配后再复查、再
回滚"，取消一个 pending 条目也只要等回合结束再摘。
"""

import threading
import types

from neu_box.scheduling.queue import TaskQueue


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
    # _wake_scheduler 会读这两个；不接真实 loop 就置 None（它自己会跳过）。
    queue._loop = None
    queue._wake = None
    queue._db = types.SimpleNamespace(
        delete_task=lambda *args: None,
        update_task_result=lambda *args, **kwargs: None,
    )
    queue._remove_log = lambda task_id: None
    return queue


def test_round_lock_excludes_every_mutation():
    """回合进行中，谁也拿不到回合锁。"""
    queue = _queue()
    started = threading.Event()
    release = threading.Event()

    def run_round():
        with queue._round_lock:
            started.set()
            release.wait(5)

    thread = threading.Thread(target=run_round, daemon=True)
    thread.start()
    assert started.wait(5), '回合没起来'

    assert queue._round_lock.acquire(timeout=0.2) is False, (
        '回合进行中不该有人能改队列'
    )
    release.set()
    thread.join(5)
    assert queue._round_lock.acquire(timeout=2) is True
    queue._round_lock.release()


def test_delete_tasks_waits_for_the_current_round():
    """删任务属于"改队列"，必须排在当前回合之后。"""
    queue = _queue()
    queue._enqueue('task', 't1', {'task_id': 't1'})

    queue._round_lock.acquire()
    done = threading.Event()

    def deleter():
        queue.delete_tasks(['t1'])
        done.set()

    threading.Thread(target=deleter, daemon=True).start()
    assert not done.wait(0.2), '删除没有等回合结束就动了队列'
    assert queue._lookup('task', 't1') is not None

    queue._round_lock.release()
    assert done.wait(2), '回合结束后删除仍然没跑完'
    assert queue._lookup('task', 't1') is None
