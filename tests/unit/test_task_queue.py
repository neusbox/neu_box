"""TaskQueue 调度策略单元测试 — first-schedulable（无队头阻塞）。

覆盖:
- 队头设备任务等卡时，后面的纯 CPU 任务不被阻塞
- 卡释放后，排在队头的设备任务正常获得执行
- 优先级语义保持：高优先级设备任务优先拿空闲卡
- 指定 device_ids 的任务等其专属卡时不阻塞其他任务
"""

from __future__ import annotations

import threading
import time

import pytest

from neu_box.database.migrations import migrate_database
from neu_box.worker.executor.db import (
    Database as WorkerDatabase,
    MIGRATIONS_PACKAGE as WORKER_MIGRATIONS,
    REQUIRED_COLUMNS as WORKER_COLUMNS,
    REQUIRED_INDEXES as WORKER_INDEXES,
)


class FakeExecutor:
    """记录执行；可选 hold 住任务（模拟长任务，便于断言 running 状态）。"""

    def __init__(self, hold: threading.Event | None = None):
        self.hold = hold
        self.devices: list = []
        self.sandbox = ''

    def run(self, timeout=None):
        if self.hold is not None:
            self.hold.wait(timeout=15)
        return {
            'returncode': 0, 'stdout': 'ok', 'stderr': '',
            'timed_out': False, 'error': None,
        }

    def cancel(self):
        if self.hold is not None:
            self.hold.set()


class FakeSbx:
    """可控制设备池的沙盒管理器替身（无 cgroup / 无子进程）。"""

    def __init__(self, devices=()):
        self._lock = threading.Lock()
        self._free = set(devices)
        self.allocated: list[tuple[str, list]] = []   # (sandbox_id, devices)
        self.destroyed: list[str] = []
        self.probes = 0                                # free_devices() 调用次数

    def free_devices(self):
        with self._lock:
            self.probes += 1
            return sorted(self._free)

    def allocate_sandbox(self, owner, sandbox_id, cpu=0, mem='0',
                         device_num=0, device_ids=None):
        with self._lock:
            if device_ids:
                if not all(d in self._free for d in device_ids):
                    return None
                devices = list(device_ids)
            elif device_num > 0:
                if len(self._free) < device_num:
                    return None
                devices = sorted(self._free)[:device_num]
            else:
                devices = []
            self._free -= set(devices)
            self.allocated.append((sandbox_id, devices))
            return {
                'sandbox_name': f'sbx_{owner}_{sandbox_id}.slice',
                'devices': devices,
            }

    def destroy_sandbox(self, name):
        with self._lock:
            self.destroyed.append(name)
            return True

    def join_sandbox(self, *args, **kwargs):
        pass

    def release(self, devices=()):
        """模拟设备释放（任务结束 / 外部进程退出）。"""
        with self._lock:
            self._free |= set(devices)


@pytest.fixture
def env(tmp_path, monkeypatch):
    database = tmp_path / "worker.db"
    migrate_database(
        database, WORKER_MIGRATIONS, WORKER_COLUMNS, WORKER_INDEXES,
    )
    monkeypatch.setenv("NEU_BOX_DB_PATH", str(database))
    monkeypatch.setenv("NEU_BOX_TASK_LOG_DIR", str(tmp_path / "task-logs"))
    monkeypatch.delenv("NEU_BOX_DEVICE_INFO_SCRIPT", raising=False)
    WorkerDatabase._instance = None

    from neu_box.worker.executor import command
    from neu_box.worker.executor.command import TaskQueue

    TaskQueue._instance = None

    fake = FakeSbx()
    monkeypatch.setattr(
        command.SbxManager, "get_instance",
        classmethod(lambda cls: fake),
    )
    executors: list[FakeExecutor] = []

    def fake_build(task, sandbox_name, devices):
        ex = FakeExecutor()
        ex.sandbox = sandbox_name
        ex.devices = list(devices)
        # 纯 CPU 任务由测试通过 cpu_holds 控制时长
        ex.hold = cpu_holds.get(task['task_id'])
        executors.append(ex)
        return ex

    cpu_holds: dict[str, threading.Event] = {}
    monkeypatch.setattr(
        TaskQueue, "_build_executor", staticmethod(fake_build),
    )
    yield command, TaskQueue, fake, executors, cpu_holds

    # 释放所有 hold、停消费者线程、恢复单例
    for hold in cpu_holds.values():
        hold.set()
    tq = TaskQueue._instance
    if tq is not None:
        tq._running_flag = False
        with tq._lock:
            tq._cv.notify_all()
        if tq._worker_thread is not None:
            tq._worker_thread.join(timeout=5)
    TaskQueue._instance = None
    WorkerDatabase._instance = None


def _status(tq, task_id):
    row = tq._db.get_task(task_id)
    return row['status'] if row else None


def _wait_status(tq, task_id, want, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _status(tq, task_id) in want:
            return _status(tq, task_id)
        time.sleep(0.05)
    return _status(tq, task_id)


def test_cpu_task_not_blocked_by_device_waiter(env):
    """队头设备任务等卡时，纯 CPU 任务应立即执行（first-schedulable）。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    # 空闲池为空：任何设备任务都拿不到卡
    tq = TaskQueue.get_instance()

    t1 = tq.submit(user_id='pengyt', command='need device', device_num=1)
    hold = threading.Event()
    cpu_holds[t2 := tq.submit(user_id='pengyt', command='cpu only')] = hold
    tq.start()  # 两个任务都入队后再启动消费者，时序确定

    got = _wait_status(tq, t2, ('running',))
    assert got == 'running', \
        f"纯 CPU 任务不应被等卡的队头任务阻塞 (status={got})"
    assert _status(tq, t1) == 'queued', "缺设备的任务应仍在排队"

    hold.set()  # 结束 CPU 任务
    assert _wait_status(tq, t2, ('completed',)) == 'completed'

    # 卡释放后，一直等卡的队头任务应被调度
    fake.release(['d0'])
    with tq._lock:
        tq._cv.notify()
    assert _wait_status(tq, t1, ('completed',)) == 'completed'
    assert (t1, ['d0']) in fake.allocated


def test_priority_task_gets_free_device_first(env):
    """空闲卡有限时，高优先级设备任务（后提交）优先拿卡。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    fake.release(['d0'])  # 仅 1 张空闲卡
    tq = TaskQueue.get_instance()

    t_low = tq.submit(
        user_id='pengyt', command='low prio dev', device_num=1, priority=0,
    )
    t_high = tq.submit(
        user_id='pengyt', command='high prio dev', device_num=1, priority=1,
    )
    tq.start()  # 同轮快照中两个任务都在 → 优先级排序生效

    assert _wait_status(tq, t_high, ('completed',)) == 'completed'
    assert _status(tq, t_low) == 'queued', "没有空闲卡了，低优先级任务应继续排队"

    fake.release(['d0'])
    with tq._lock:
        tq._cv.notify()
    assert _wait_status(tq, t_low, ('completed',)) == 'completed'


def test_pinned_device_task_does_not_block_others(env):
    """指定 device_ids 的任务等其专属卡时，不阻塞能用其他卡的任务。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    fake.release(['d1'])  # 只有 d1 空闲
    tq = TaskQueue.get_instance()

    t_pin = tq.submit(
        user_id='pengyt', command='pinned to d0', device_ids=['d0'],
    )
    t_any = tq.submit(
        user_id='pengyt', command='any device', device_num=1,
    )
    tq.start()

    assert _wait_status(tq, t_any, ('completed',)) == 'completed'
    assert _status(tq, t_pin) == 'queued', "等待指定卡的任务应继续排队"

    fake.release(['d0'])
    with tq._lock:
        tq._cv.notify()
    assert _wait_status(tq, t_pin, ('completed',)) == 'completed'
    assert (t_pin, ['d0']) in fake.allocated


def test_cpu_only_queue_never_probes_devices(env):
    """全 CPU 队列不应触发设备探测（不跑昂贵的 npu-smi 脚本）。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    tq = TaskQueue.get_instance()

    t1 = tq.submit(user_id='pengyt', command='cpu 1')
    t2 = tq.submit(user_id='pengyt', command='cpu 2')
    tq.start()

    assert _wait_status(tq, t1, ('completed',)) == 'completed'
    assert _wait_status(tq, t2, ('completed',)) == 'completed'
    assert fake.probes == 0, f"纯 CPU 队列不应探测设备池 (probes={fake.probes})"


def test_fifo_within_same_priority_when_resources_available(env):
    """资源充足时，同优先级保持 FIFO。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    fake.release(['d0', 'd1', 'd2'])
    tq = TaskQueue.get_instance()

    t1 = tq.submit(user_id='pengyt', command='first', device_num=1)
    t2 = tq.submit(user_id='pengyt', command='second', device_num=1)
    t3 = tq.submit(user_id='pengyt', command='third', device_num=1)
    tq.start()

    assert _wait_status(tq, t1, ('completed',)) == 'completed'
    assert _wait_status(tq, t2, ('completed',)) == 'completed'
    assert _wait_status(tq, t3, ('completed',)) == 'completed'
    # 先提交的任务先拿到卡（fake.allocated 按分配顺序记录 task_id）
    order = [task_id for task_id, _ in fake.allocated]
    assert order.index(t1) < order.index(t2) < order.index(t3)
