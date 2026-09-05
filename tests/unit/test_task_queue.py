"""TaskQueue 排队规则单元测试 — 纯 CPU 任务排在申请设备的任务之前。

设计：消费者逻辑保持原样（只处理队头，优先级 DESC → FIFO），
只在排队顺序上把不申请异构卡的任务（device_num=0 且未指定
device_ids）统一提到所有申请设备的任务之前，避免等卡任务占住
队头时纯 CPU 任务被一起阻塞。
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
        self.allocated: list[tuple[str, list]] = []   # (task_id, devices)
        self.destroyed: list[str] = []

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


def test_cpu_task_lines_ahead_of_device_task(env):
    """纯 CPU 任务后提交也排在先提交的设备任务之前（position 更小）。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    fake.release(['d0'])
    tq = TaskQueue.get_instance()

    t_dev = tq.submit(user_id='pengyt', command='needs card', device_num=1)
    t_cpu = tq.submit(user_id='pengyt', command='cpu only')

    with tq._lock:
        pos = {tid: t['position'] for tid, t in tq._pending.items()}
    assert pos[t_cpu] == 1, f"纯 CPU 任务应在队首 (position={pos[t_cpu]})"
    assert pos[t_dev] == 2, f"设备任务应排在其后 (position={pos[t_dev]})"

    tq.start()
    assert _wait_status(tq, t_cpu, ('completed',)) == 'completed'
    assert _wait_status(tq, t_dev, ('completed',)) == 'completed'


def test_cpu_task_runs_while_device_task_waits(env):
    """队头位置被 CPU 任务占据时，等卡任务不阻塞 CPU 任务执行。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    # 空闲池为空：设备任务拿不到卡
    tq = TaskQueue.get_instance()

    t_dev = tq.submit(user_id='pengyt', command='needs card', device_num=1)
    hold = threading.Event()
    cpu_holds[t_cpu := tq.submit(user_id='pengyt', command='cpu only')] = hold
    tq.start()

    got = _wait_status(tq, t_cpu, ('running',))
    assert got == 'running', f"纯 CPU 任务应能执行 (status={got})"
    assert _status(tq, t_dev) == 'queued', "缺设备的任务应仍在排队"

    hold.set()  # 结束 CPU 任务
    assert _wait_status(tq, t_cpu, ('completed',)) == 'completed'

    # 卡释放后，设备任务正常获得执行
    fake.release(['d0'])
    with tq._lock:
        tq._cv.notify()
    assert _wait_status(tq, t_dev, ('completed',)) == 'completed'
    assert (t_dev, ['d0']) in fake.allocated


def test_cpu_task_beats_high_priority_device_task(env):
    """「提到最前面」对高优先级设备任务同样生效。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    tq = TaskQueue.get_instance()

    t_high = tq.submit(
        user_id='pengyt', command='high prio dev', device_num=1, priority=1,
    )
    t_cpu = tq.submit(user_id='pengyt', command='cpu only', priority=0)

    with tq._lock:
        pos = {tid: t['position'] for tid, t in tq._pending.items()}
    assert pos[t_cpu] < pos[t_high], "纯 CPU 任务应排在高优先级设备任务前"


def test_priority_and_fifo_within_groups(env):
    """分组内部保持原有语义：优先级 DESC → FIFO。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    fake.release(['d0', 'd1', 'd2', 'd3'])
    tq = TaskQueue.get_instance()

    # 设备组：低优先级先提交，高优先级后提交
    d_low = tq.submit(
        user_id='pengyt', command='d low', device_num=1, priority=0,
    )
    d_high = tq.submit(
        user_id='pengyt', command='d high', device_num=1, priority=1,
    )
    # CPU 组：低优先级先提交，高优先级后提交
    c_low = tq.submit(user_id='pengyt', command='c low', priority=0)
    c_high = tq.submit(user_id='pengyt', command='c high', priority=1)

    with tq._lock:
        order = [
            tid for tid in sorted(
                tq._pending, key=lambda tid: tq._sort_key(tq._pending[tid]),
            )
        ]
    # CPU 组在前（c_high 优先级更高），设备组在后（d_high 优先级更高）
    assert order == [c_high, c_low, d_high, d_low], f"顺序不符: {order}"


def test_pinned_device_task_still_waits_for_its_device(env):
    """指定 device_ids 的任务属于设备任务组，等其专属卡。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    fake.release(['d1'])
    tq = TaskQueue.get_instance()

    t_pin = tq.submit(user_id='pengyt', command='pinned d0', device_ids=['d0'])
    t_dev = tq.submit(user_id='pengyt', command='any dev', device_num=1)

    with tq._lock:
        pos = {tid: t['position'] for tid, t in tq._pending.items()}
    # 两个都是设备任务 → 按 FIFO：pinned 在前
    assert pos[t_pin] < pos[t_dev]

    tq.start()
    # pinned 等 d0（未释放），原逻辑下它占住队头：t_dev 暂不执行
    time.sleep(0.5)
    assert _status(tq, t_pin) == 'queued'
    assert _status(tq, t_dev) == 'queued', "原逻辑：队头等待时不跳过（保持不变）"

    fake.release(['d0'])
    with tq._lock:
        tq._cv.notify()
    assert _wait_status(tq, t_pin, ('completed',)) == 'completed'
    assert _wait_status(tq, t_dev, ('completed',)) == 'completed'


def test_fifo_within_cpu_group_when_resources_available(env):
    """资源充足时，纯 CPU 任务组内保持 FIFO。"""
    _command, TaskQueue, fake, executors, cpu_holds = env
    tq = TaskQueue.get_instance()

    t1 = tq.submit(user_id='pengyt', command='cpu first')
    t2 = tq.submit(user_id='pengyt', command='cpu second')
    t3 = tq.submit(user_id='pengyt', command='cpu third')
    tq.start()

    assert _wait_status(tq, t1, ('completed',)) == 'completed'
    assert _wait_status(tq, t2, ('completed',)) == 'completed'
    assert _wait_status(tq, t3, ('completed',)) == 'completed'
    order = [task_id for task_id, _ in fake.allocated]
    assert order == [t1, t2, t3], f"CPU 组内应保持 FIFO: {order}"
