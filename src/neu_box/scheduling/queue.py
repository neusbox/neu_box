"""Worker 的异步调度器。

调度器只有一个后台事件循环；命令执行是 asyncio task，不再为每个任务
创建 Python 线程。SQLite 保存任务结果和恢复所需的最小状态，队列顺序只
存在内存中，队列重排不会逐条 UPDATE 数据库。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from typing import Callable

from neu_box.config import env_int
from neu_box.execution import logs
from neu_box.execution.host import command_timeout, HostCommandExecutor
from neu_box.execution.docker import DockerCommandExecutor
from neu_box.execution.target import TARGET_HOST
from neu_box.storage import Database
from neu_box.runtime import devices
from neu_box.runtime.sandbox import SandboxAllocationPaused, SbxManager
from neu_box.scheduling import resources, tasks as task_shape
from neu_box.scheduling.scheduler import SchedulerMixin
from neu_box.scheduling.sessions import AcquireFailure, AcquireRequest

logger = logging.getLogger(__name__)
MAX_COMPLETED_TASKS = env_int("NEU_BOX_COMMAND_MAX_COMPLETED", 200)
QUEUE_RECENT_LIMIT = env_int("NEU_BOX_COMMAND_QUEUE_RECENT", 30)


class TaskQueue(SchedulerMixin):
    """单 Worker 调度队列，任务和 acquire 共用一个异步出队循环。"""

    _instance = None

    def __init__(self):
        self._lock = threading.RLock()
        # 排队顺序**就是**这里：priority=1 桶整桶先于 priority=0 桶，桶内是插入序。
        #
        # 用 dict 而不是 list/deque：需要按 (kind, identifier) 随机删（删任务、
        # 取消、acquire release），而 Python 3.7+ 的 dict 保插入序、删除是 O(1)。
        # 键是 ``(kind, identifier)``：kind 取 ``'task'`` 或 ``'acquire'``，
        # 两类共用同一套队列顺序，只在派发时分支。
        self._queues: dict[int, dict[tuple[str, str], object]] = {1: {}, 0: {}}
        self._acquire_results: dict[str, tuple[dict | None, Exception | None]] = {}
        # acquire 成功后并不是一次性请求：终端会一直占用 sandbox，直到
        # release 或 reaper 回收。这里保留活跃记录，避免 pause 只看到命令
        # 任务已经清空就误判为 quiet。
        self._running_acquires: dict[str, dict] = {}
        self._running: dict[str, dict] = {}
        self._active: set[asyncio.Task] = set()
        self._maintenance_errors: dict[str, str] = {}
        # 显式 pause 请求之后置位，直到进程退出（见 set_paused）。
        self._pause_in_progress = False
        self._dispatching = 0
        self._running_flag = False
        self._scheduler_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._db = Database.get_instance()
        self._recover_orphaned()

    @classmethod
    def get_instance(cls) -> 'TaskQueue':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 队列结构 ─────────────────────────────────────────────────────
    # 只有四个操作：入队、出队（按 id）、查找（按 id）、按顺序遍历。
    # 顺序在**入队时**就定死了，调度时只读不动。

    def _enqueue(self, kind: str, identifier: str, value, priority: int = 0):
        """入队。``priority`` 非 0 进高优先桶（1），否则进普通桶（0）。"""
        self._queues[1 if priority else 0][(kind, identifier)] = value

    def _dequeue(self, kind: str, identifier: str) -> bool:
        """按 id 出队；不在队列里返回 False。"""
        for bucket in self._queues.values():
            if bucket.pop((kind, identifier), None) is not None:
                return True
        return False

    def _lookup(self, kind: str, identifier: str):
        """按 id 取条目（不出队）；不在队列里返回 None。"""
        for priority in (1, 0):
            value = self._queues[priority].get((kind, identifier))
            if value is not None:
                return value
        return None

    def _ordered(self) -> list[tuple[tuple[str, str], object]]:
        """全部条目，按调度顺序（1 桶整桶在前，桶内插入序）。"""
        return list(self._queues[1].items()) + list(self._queues[0].items())

    def _pending_items(self, kind: str) -> list[tuple[str, object]]:
        """某一类条目，按调度顺序：``[(identifier, value), ...]``。"""
        return [
            (identifier, value)
            for (entry_kind, identifier), value in self._ordered()
            if entry_kind == kind
        ]

    def _count(self, kind: str) -> int:
        return sum(
            1 for bucket in self._queues.values()
            for entry_kind, _identifier in bucket if entry_kind == kind
        )

    def _recover_orphaned(self):
        """只将 pending task 重新入队；遗留 running task 标记失败并收尾。"""
        recovered = []
        for task in self._db.get_queue_tasks():
            if task['status'] == 'running':
                logger.warning('恢复: 标记孤儿任务 %s 为 failed', task['task_id'])
                self._db.update_task_result(
                    task['task_id'], 'failed', -1, '', '',
                    error='Worker 可能在执行过程中重启',
                )
                # The worker may have died while the command process was
                # still inside its sandbox.  Marking the DB row failed alone
                # would leave a populated cgroup that the normal empty-only
                # reaper can never collect.  Task sandboxes are disposable
                # after an orphaned execution, so terminate and destroy them
                # during recovery; a failure remains visible to maintenance.
                sandbox_name = task_shape.sandbox_name(task)
                try:
                    if not SbxManager.get_instance().destroy_sandbox(sandbox_name):
                        self._maintenance_errors[sandbox_name] = (
                            '孤儿任务 sandbox 清理失败，将在后续维护中重试'
                        )
                except Exception as exc:
                    self._maintenance_errors[sandbox_name] = (
                        f'孤儿任务 sandbox 清理异常: {exc}'
                    )
                    logger.exception('恢复时清理孤儿 sandbox 失败: %s', sandbox_name)
                continue
            task['target'] = task.get('target_spec') or {'type': TARGET_HOST}
            task['devices'] = []
            recovered.append(task)
        # 队列顺序是「优先级 DESC → FIFO」，而 DB 的返回顺序不保证；重启恢复是
        # 唯一不走 submit() 的入队路径，所以在这里自己排一遍。
        for task in sorted(recovered, key=task_shape.sort_key):
            self._enqueue('task', task['task_id'], task, task.get('priority', 0))

    def submit(self, user_id: str, command: str, cpu: int = 0,
               mem: str = '0', device_num: int = 0,
               device_ids: list | None = None, target: dict | None = None,
               est_time: int = 0, priority: int = 0) -> str:
        if SbxManager.get_instance().allocations_paused():
            raise SandboxAllocationPaused('Worker 处于暂停维护状态，暂不接受新任务')
        task_id = uuid.uuid4().hex[:12]
        task = {
            'task_id': task_id, 'user_id': user_id, 'command': command,
            'cpu': cpu, 'mem': mem, 'device_num': device_num,
            'device_ids': list(device_ids or []),
            'target': dict(target or {'type': TARGET_HOST}),
            'est_time': est_time, 'priority': priority, 'devices': [],
            'status': 'queued', 'position': 0, 'created_at': time.time(),
            'started_at': None, 'finished_at': None, 'result': None,
        }
        with self._lock:
            if SbxManager.get_instance().allocations_paused():
                raise SandboxAllocationPaused('Worker 处于暂停维护状态，暂不接受新任务')
            self._db.insert_task(
                task_id, user_id, command, cpu, mem, [], 0, device_num,
                device_ids or [], task['target'], est_time, priority,
            )
            self._enqueue('task', task_id, task, priority)
        self._wake_scheduler()
        logger.info('任务入队: %s user=%s priority=%s', task_id, user_id, priority)
        return task_id

    def enqueue_acquire(self, owner: str, pid: int, cpu: int, mem: str,
                        device_num: int, device_ids: list[str] | None = None,
                        device_root: str | None = None,
                        validator: Callable[[], None] | None = None,
                        priority: int = 0) -> AcquireRequest:
        """把 acquire 放入同一调度队列，返回可轮询的请求对象。"""
        if not self._running_flag:
            raise AcquireFailure('Worker 调度器尚未启动', 'scheduler_unavailable')
        if SbxManager.get_instance().allocations_paused():
            raise SandboxAllocationPaused('Worker 处于暂停维护状态，暂不创建新沙盒')
        request = AcquireRequest(
            request_id=uuid.uuid4().hex[:12], owner=owner, pid=pid,
            cpu=cpu, mem=mem, device_num=device_num,
                device_ids=list(device_ids or []), device_root=device_root,
            validator=validator,
            created_at=time.time(),
            priority=priority,
        )
        with self._lock:
            if SbxManager.get_instance().allocations_paused():
                raise SandboxAllocationPaused('Worker 处于暂停维护状态，暂不创建新沙盒')
            self._enqueue(
                'acquire', request.request_id, request, request.priority,
            )
        self._wake_scheduler()
        return request

    def acquire(self, owner: str, pid: int, cpu: int, mem: str,
                device_num: int, device_ids: list[str] | None = None,
                device_root: str | None = None,
                validator: Callable[[], None] | None = None,
                priority: int = 0) -> dict:
        """同步适配入口；HTTP API 使用 ``enqueue_acquire`` 避免占满服务线程。"""
        request = self.enqueue_acquire(
            owner, pid, cpu, mem, device_num, device_ids, device_root, validator,
            priority,
        )
        return request.result.result()

    def acquire_result(self, request_id: str, consume: bool = False):
        """返回 acquire 状态：queued、completed 或 failed。"""
        with self._lock:
            request = self._lookup('acquire', request_id)
            if request is not None:
                return {'status': 'queued'}
            result = self._acquire_results.get(request_id)
            if result is None:
                return None
            value, error = result
            if consume:
                self._acquire_results.pop(request_id, None)
        if error is not None:
            return {
                'status': 'failed',
                'error': str(error),
                'code': getattr(error, 'code', 'sandbox_acquire_failed'),
            }
        return {'status': 'completed', 'result': value}

    async def _dispatch(self, kind: str, identifier: str):
        sbx = SbxManager.get_instance()
        if kind == 'acquire':
            with self._lock:
                request = self._lookup('acquire', identifier)
            if request is None:
                return
            try:
                allocated = None
                if request.validator:
                    request.validator()
                allocated = resources.allocate(
                    request.owner, str(request.pid), request.cpu, request.mem,
                    request.device_num, request.device_ids or None,
                )
                if allocated is None:
                    # 预判过了、分配时又拿不到（探测到分配之间的窗口）：这一轮
                    # 跳过它，下一轮重扫。**不打退避** —— 队列里的条目原样留着。
                    return
                if request.device_root:
                    visible = set(devices.discover_nodes(request.device_root))
                    missing = sorted(set(allocated['devices']) - visible)
                    if missing:
                        raise AcquireFailure(
                            f'目标容器没有挂载沙盒设备节点: {missing}',
                            'docker_devices_not_visible',
                        )
                # Serialize the join and running transition with pause.  A
                # successful join makes this a long-lived acquire: pause must
                # then wait for its release instead of cancelling its Future
                # and destroying the terminal's new cgroup.
                with self._lock:
                    cancelled = self._lookup('acquire', identifier) is None
                    paused = sbx.allocations_paused()
                    if not cancelled and not paused:
                        if not sbx.join_sandbox(
                            allocated['sandbox_name'], request.pid,
                            borrowed=True,
                        ):
                            raise AcquireFailure('加入沙盒失败', 'sandbox_join_failed')
                        self._dequeue('acquire', identifier)
                        self._running_acquires[allocated['sandbox_name']] = {
                            'request_id': identifier,
                            'owner': request.owner,
                            'pid': request.pid,
                            'sandbox_name': allocated['sandbox_name'],
                            'devices': list(allocated.get('devices') or []),
                            'created_at': request.created_at,
                            'acquired_at': time.time(),
                        }
                        self._acquire_results[identifier] = (allocated, None)
                if cancelled or paused:
                    sbx.destroy_sandbox(allocated['sandbox_name'])
                    error = AcquireFailure(
                        'Worker 进入暂停维护，acquire 请求已取消',
                        'worker_paused',
                    )
                    with self._lock:
                        self._dequeue('acquire', identifier)
                        self._acquire_results[identifier] = (None, error)
                    if not request.result.done():
                        request.result.set_exception(error)
                    return
                request.result.set_result(allocated)
            except Exception as exc:
                if isinstance(exc, SandboxAllocationPaused):
                    return
                if allocated is not None:
                    try:
                        sbx.destroy_sandbox(allocated['sandbox_name'])
                    except Exception:
                        logger.exception('acquire 失败后的 sandbox 回收失败')
                with self._lock:
                    self._dequeue('acquire', identifier)
                    self._acquire_results[identifier] = (None, exc)
                if not request.result.done():
                    request.result.set_exception(exc)
            return

        with self._lock:
            task = self._lookup('task', identifier)
        if task is None:
            return
        allocated = None
        try:
            allocated = resources.allocate(
                task['user_id'], task['task_id'], task.get('cpu', 0),
                task.get('mem', '0'), task.get('device_num', 0),
                task.get('device_ids') or None,
            )
            if allocated is None:
                # 同 acquire：预判过了又拿不到 → 这轮跳过，下轮重扫，不打退避。
                return
            with self._lock:
                # Serialize the maintenance gate with the transition from
                # pending to running. If pause acquired this lock first, the
                # allocation is rolled back; if dispatch acquired it first,
                # pause observes the running task and waits for completion.
                paused = sbx.allocations_paused()
                if paused:
                    task = None
                else:
                    # 出队（条目可能在 precheck 之后被删/取消）
                    task = self._lookup('task', identifier)
                    if task is not None:
                        self._dequeue('task', identifier)
                if task is not None:
                    task['status'] = 'running'
                    task['started_at'] = time.time()
                    task['devices'] = allocated['devices']
                    task['_executor'] = self._build_executor(
                        task, allocated['sandbox_name'],
                    )
                    self._running[identifier] = task
            if task is None:
                sbx.destroy_sandbox(allocated['sandbox_name'])
                return
            self._db.update_task_status(
                identifier, 'running', started_at=task['started_at'],
                devices=task['devices'],
            )
            execution = asyncio.create_task(self._execute_one(task))
            self._active.add(execution)
            execution.add_done_callback(self._execution_done)
        except Exception as exc:
            if isinstance(exc, SandboxAllocationPaused):
                return
            logger.exception('任务 %s 启动失败', identifier)
            try:
                sandbox_name = (allocated['sandbox_name'] if allocated
                                else task_shape.sandbox_name(task))
                sbx.destroy_sandbox(sandbox_name)
            except Exception:
                logger.exception('任务 %s 启动失败后的 sandbox 回收失败', identifier)
            self._finish_start_failure(task, exc)

    def _execution_done(self, execution: asyncio.Task):
        self._active.discard(execution)
        try:
            execution.result()
        except Exception:
            logger.exception('异步任务执行协程异常')

    def _build_executor(self, task: dict, sandbox_name: str):
        if task.get('target', {}).get('type', TARGET_HOST) == TARGET_HOST:
            return HostCommandExecutor(task=task, sandbox_name=sandbox_name)
        return DockerCommandExecutor(
            task=task, sandbox_name=sandbox_name,
            devices=task.get('devices', []),
            log_path=logs.log_path(task['task_id']),
        )

    async def _execute_one(self, task: dict):
        task_id = task['task_id']
        sandbox_name = task_shape.sandbox_name(task)
        try:
            executor = task['_executor']
            # 两个后端形状一样（execution/process.py 的 CommandBackend）：
            # host 本来就跑在事件循环里，docker 自己把阻塞调用包进线程。
            result = await executor.run(command_timeout())
        except Exception as exc:
            logger.exception('任务 %s 异常', task_id)
            result = {
                'returncode': -1, 'timed_out': False,
                'error': f'执行器异常: {exc}',
            }

        try:
            cleanup_ok = SbxManager.get_instance().destroy_sandbox(sandbox_name)
        except Exception as exc:
            cleanup_ok = False
            logger.exception('任务 %s 清理 sandbox 失败', task_id)
        if not cleanup_ok:
            result = {**(result or {}), 'returncode': -1,
                      'error': 'sandbox_cleanup_failed'}
        if task.get('_canceled'):
            result = {**(result or {}), 'returncode': -1,
                      'timed_out': False, 'error': '用户手动取消'}
        status = ('completed' if result.get('returncode') == 0
                  and not result.get('timed_out') else 'failed')
        finished_at = time.time()
        try:
            self._db.update_task_result(
                task_id, status, result.get('returncode', -1), '', '',
                result.get('timed_out', False), result.get('error'), finished_at,
            )
            self._db.cleanup_old_tasks(keep=MAX_COMPLETED_TASKS)
        except Exception as exc:
            with self._lock:
                self._maintenance_errors[task_id] = f'保存任务结果失败: {exc}'
            logger.exception('保存任务 %s 结果失败', task_id)
        with self._lock:
            task.update(result=result, status=status, finished_at=finished_at)
            self._running.pop(task_id, None)
        logger.info('执行完成: %s status=%s', task_id, status)

    def _finish_start_failure(self, task: dict, exc: Exception):
        if not task:
            return
        task_id = task['task_id']
        error = ('用户手动取消' if task.get('_canceled')
                 else 'sandbox 或执行器启动失败: ' + str(exc))
        try:
            self._db.update_task_result(task_id, 'failed', -1, '', '', error=error)
        except Exception as db_exc:
            with self._lock:
                self._maintenance_errors[task_id] = f'保存任务结果失败: {db_exc}'
        with self._lock:
            self._dequeue('task', task_id)
            self._running.pop(task_id, None)

    def delete_tasks(self, task_ids: list[str]) -> int:
        deleted = 0
        cancel = []
        with self._lock:
            for task_id in task_ids:
                if self._lookup('task', task_id) is not None:
                    self._dequeue('task', task_id)
                    cancel.append(('delete', task_id))
                elif task_id in self._running:
                    self._running[task_id]['_canceled'] = True
                    cancel.append(('cancel', self._running[task_id]))
                else:
                    cancel.append(('delete', task_id))
        for action, value in cancel:
            if action == 'delete':
                self._db.delete_task(value)
                self._remove_log(value)
            else:
                try:
                    value['_executor'].cancel()
                except Exception:
                    logger.exception('取消任务 %s 失败', value['task_id'])
            deleted += 1
        self._wake_scheduler()
        return deleted

    @staticmethod
    def _remove_log(task_id: str):
        logs.remove(task_id)

    def get_queue(self) -> list[dict]:
        active = self._db.get_queue_tasks()
        recent = self._db.get_recent_tasks(limit=QUEUE_RECENT_LIMIT)
        queued = [task for task in active if task['status'] == 'queued']
        queued.sort(key=task_shape.sort_key)
        eta = 0
        for position, task in enumerate(queued, 1):
            task['position'] = position
            task['eta'] = eta
            eta += task.get('est_time', 0) or 0
        running = [task for task in active if task['status'] == 'running']
        active = running + queued
        active_ids = {task['task_id'] for task in active}
        return [task_shape.public(task) for task in active + [
            task for task in recent if task['task_id'] not in active_ids
        ]]

    def position(self, task_id: str) -> int:
        with self._lock:
            for position, (identifier, _task) in enumerate(
                    self._pending_items('task'), 1):
                if identifier == task_id:
                    return position
        return 0

    def pending_count(self) -> int:
        with self._lock:
            return self._count('task')

    def get_result(self, task_id: str) -> dict | None:
        task = self._db.get_task(task_id)
        if task is None:
            return None
        result = task_shape.public(task)
        result['result'] = {
            'returncode': task.get('returncode'),
            'timed_out': bool(task.get('timed_out')),
            'error': task.get('error'),
        }
        return result

    def set_paused(self, paused: bool, maintenance_request: bool = False) -> dict:
        """暂停/恢复调度。

        ``maintenance_request`` 只在收到显式的 pause 请求时为真：那表示
        一次停机维护已经开始，这一次 pause 之后本进程不再接受 resume ——
        运维要么等 pause 走完停服，要么重启服务（重启后看到 .paused 标记
        会自己起来处于暂停态，那时 resume 才是合法的）。服务启动时按标记
        恢复暂停态不经过这里，所以不算"维护进行中"。
        """
        with self._lock:
            if paused and maintenance_request:
                self._pause_in_progress = True
            SbxManager.get_instance().set_allocations_paused(paused)
            if paused:
                self._cancel_pending_acquires_locked()
        self._wake_scheduler()
        return self.maintenance_status()

    def maintenance_in_progress(self) -> bool:
        with self._lock:
            return self._pause_in_progress

    def _cancel_pending_acquires_locked(self):
        """取消尚未分配 sandbox 的 acquire。

        acquire 绑定了调用方的 PID，不能像 queued task 一样跨停机恢复。
        pause 时将它们转为可轮询的失败结果，客户端可在 setup/resume 后
        重新发起申请。调用者必须持有 ``_lock``。
        """
        pending = list(self._pending_items('acquire'))
        for request_id, request in pending:
            error = AcquireFailure(
                'Worker 进入暂停维护，acquire 请求已取消',
                'worker_paused',
            )
            self._dequeue('acquire', request_id)
            self._acquire_results[request_id] = (None, error)
            if not request.result.done():
                request.result.set_exception(error)

    def _reconcile_running_acquires(self):
        """移除已被 sandbox reaper 清理的 acquire 记录。"""
        with self._lock:
            stale = [
                name for name in self._running_acquires
                if self._db.get_sandbox(name) is None
            ]
            for name in stale:
                self._running_acquires.pop(name, None)

    def has_running_acquire(self, sandbox_name: str) -> bool:
        self._reconcile_running_acquires()
        with self._lock:
            return sandbox_name in self._running_acquires

    def release_acquire(self, sandbox_name: str):
        """释放由 acquire 持有的 sandbox，并在成功后结束 running 状态。

        归还借来的终端、收掉沙盒里长出来的进程和挂靠的容器，全部在
        ``SbxManager.destroy_sandbox`` 内部按顺序完成 —— 调用方只需要报
        沙盒名。返回 ``None`` 表示该 sandbox 不是活跃 acquire，
        ``True``/``False`` 表示已知 acquire 的销毁结果。
        """
        self._reconcile_running_acquires()
        with self._lock:
            record = self._running_acquires.get(sandbox_name)
        if record is None:
            return None

        ok = SbxManager.get_instance().destroy_sandbox(sandbox_name)
        if ok:
            with self._lock:
                self._running_acquires.pop(sandbox_name, None)
        return bool(ok)

    def maintenance_status(self) -> dict:
        self._reconcile_running_acquires()
        with self._lock:
            pending = self._count('task')
            pending_acquires = self._count('acquire')
            running = len(self._running)
            running_acquires = len(self._running_acquires)
            dispatching = self._dispatching
            errors = dict(self._maintenance_errors)
            pause_in_progress = self._pause_in_progress
        allocation = SbxManager.get_instance().allocation_status()
        try:
            lifecycle = SbxManager.get_instance().lifecycle_status()
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            lifecycle = None
            errors['lifecycle'] = str(exc)
        return {
            'pending_tasks': pending,
            'pending_acquires': pending_acquires,
            'running_tasks': running,
            'running_acquires': running_acquires,
            'running_total': running + running_acquires,
            # 兼容维护脚本使用的聚合字段；running_tasks 仍只表示命令任务。
            'running': running + running_acquires,
            'dispatching': dispatching,
            'maintenance_errors': errors,
            'pause_in_progress': pause_in_progress,
            'paused': allocation['paused'],
            'allocations_in_flight': allocation['in_flight'],
            'sandbox_lifecycle': lifecycle,
            'quiet': (
                allocation['paused'] and allocation['in_flight'] == 0
                and running == 0 and running_acquires == 0
                and dispatching == 0
                and not errors and lifecycle is not None
                and not lifecycle['residuals']
            ),
        }
