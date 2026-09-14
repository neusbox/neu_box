"""调度队列的顺序与挑选逻辑（两个有序桶 + first-schedulable）。

这里只测"挑哪一个、按什么顺序挑、探测几次"——这些是纯内存判断，
不需要 root、不需要 Docker、不需要真的跑 npu-smi。
"""

import asyncio
import threading

from neu_box.runtime.sandbox import SbxManager
from neu_box.scheduling import resources
from neu_box.scheduling.queue import TaskQueue
from neu_box.scheduling.sessions import AcquireRequest


def _queue():
    """绕开 __init__（它要连 DB / 跑恢复），只搭出队列结构。"""
    queue = TaskQueue.__new__(TaskQueue)
    queue._lock = threading.RLock()
    queue._queues = {1: {}, 0: {}}
    return queue


def _task(task_id, *, priority=0, device_ids=(), device_num=0, created=0.0):
    return {
        'task_id': task_id, 'priority': priority,
        'device_ids': list(device_ids), 'device_num': device_num,
        'created_at': created,
    }


def _acquire(request_id, *, priority=0, device_ids=(), device_num=0):
    return AcquireRequest(
        request_id=request_id, owner='root', pid=1234, cpu=0, mem='0',
        device_num=device_num, device_ids=list(device_ids),
        device_root=None, validator=None, created_at=0.0, priority=priority,
    )


class _FakeSbx:
    """只回答"分配有没有被暂停"。"""

    def allocations_paused(self):
        return False


def _install(monkeypatch, *, free):
    """把 SbxManager 和 free_devices 换成可控替身，并数探测次数。"""
    calls = []

    def free_devices(*args, **kwargs):
        calls.append(1)
        return list(free)

    monkeypatch.setattr(SbxManager, 'get_instance', staticmethod(lambda: _FakeSbx()))
    monkeypatch.setattr(resources, 'free_devices', free_devices)
    return calls


def test_high_priority_bucket_is_picked_first(monkeypatch):
    _install(monkeypatch, free=[])
    queue = _queue()
    queue._enqueue('task', 'low', _task('low', priority=0), 0)
    queue._enqueue('task', 'high', _task('high', priority=1), 1)
    queue._enqueue('task', 'low2', _task('low2', priority=0), 0)

    assert queue._pick_schedulable() == ('task', 'high')
    assert queue._pick_schedulable() == ('task', 'high')   # 挑选不出队


def test_fifo_within_a_bucket(monkeypatch):
    _install(monkeypatch, free=[])
    queue = _queue()
    for task_id in ('a', 'b', 'c'):
        queue._enqueue('task', task_id, _task(task_id), 0)

    assert [i for i, _v in queue._pending_items('task')] == ['a', 'b', 'c']


def test_head_waiting_for_a_card_does_not_block_the_rest(monkeypatch):
    """first-schedulable：队首要的卡被占，后面的任务照跑。"""
    _install(monkeypatch, free=['234:4'])
    queue = _queue()
    # 队首（更高优先级）要 234:0，而 234:0 没空
    queue._enqueue('task', 'blocked', _task('blocked', priority=1,
                                            device_ids=['234:0']), 1)
    # 后面这个只要 234:4
    queue._enqueue('task', 'ok', _task('ok', device_ids=['234:4']), 0)
    # 纯 CPU 的也排在后面
    queue._enqueue('task', 'cpu', _task('cpu'), 0)

    assert queue._pick_schedulable() == ('task', 'ok')


def test_pure_cpu_queue_never_probes_devices(monkeypatch):
    """全是不要卡的任务时，一轮 npu-smi 都不该跑。"""
    calls = _install(monkeypatch, free=[])
    queue = _queue()
    queue._enqueue('task', 'cpu', _task('cpu'), 0)

    assert queue._pick_schedulable() == ('task', 'cpu')
    assert calls == [], '纯 CPU 队列不该探测设备池'


def test_device_queue_probes_exactly_once_per_pick(monkeypatch):
    calls = _install(monkeypatch, free=['234:0'])
    queue = _queue()
    queue._enqueue('task', 'a', _task('a', device_ids=['234:0']), 0)
    queue._enqueue('task', 'b', _task('b', device_ids=['234:0']), 0)

    assert queue._pick_schedulable() == ('task', 'a')
    assert len(calls) == 1, '一轮只探测一次'


def test_acquire_uses_its_own_priority_bucket(monkeypatch):
    _install(monkeypatch, free=[])
    queue = _queue()
    queue._enqueue('task', 'normal', _task('normal', priority=0), 0)
    queue._enqueue('acquire', 'req', _acquire('req', priority=1), 1)

    assert queue._pick_schedulable() == ('acquire', 'req')


def test_dequeue_and_lookup_work_across_buckets():
    queue = _queue()
    queue._enqueue('task', 'low', _task('low'), 0)
    queue._enqueue('task', 'high', _task('high', priority=1), 1)

    assert queue._lookup('task', 'high')['task_id'] == 'high'
    assert queue._dequeue('task', 'high') is True
    assert queue._lookup('task', 'high') is None
    assert queue._count('task') == 1
    assert queue._dequeue('task', 'missing') is False


def test_consume_loop_lets_spawned_coroutines_run():
    """每轮让权：``_dispatch`` 里 create_task 出去的协程真的会被执行。

    这是 701 秒那个 bug 的回归测试 —— 只要主循环不让权，这个协程永远不会跑。
    """
    queue = _queue()
    queue._running_flag = True
    queue._loop = None
    queue._wake = None
    queue._active = set()
    ran = []
    entries = [('task', 't1')]

    async def work():
        ran.append('ran')

    def pick():
        if entries:
            return entries.pop()
        queue._running_flag = False
        return None

    async def dispatch(kind, identifier):
        task = asyncio.create_task(work())
        queue._active.add(task)
        task.add_done_callback(queue._active.discard)

    queue._pick_schedulable = pick
    queue._dispatch = dispatch

    asyncio.run(queue._consume_loop())
    assert ran == ['ran'], '主循环没让权，派发出去的协程没跑'
