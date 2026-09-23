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
from neu_box.storage import (
    Database,
    SESSION_ACTIVE,
    SESSION_ALLOCATING,
    SESSION_CANCELLED,
    SESSION_FAILED,
    SESSION_QUEUED,
    SESSION_RELEASED,
)
from neu_box.runtime import devices
from neu_box.runtime.sandbox import SandboxAllocationPaused, SbxManager
from neu_box.scheduling import entries, resources, tasks as task_shape
from neu_box.scheduling import sessions as session_shape
from neu_box.scheduling.scheduler import SchedulerMixin
from neu_box.scheduling.sessions import AcquireFailure, AcquireRequest

logger = logging.getLogger(__name__)
MAX_COMPLETED_TASKS = env_int("NEU_BOX_COMMAND_MAX_COMPLETED", 200)
QUEUE_RECENT_LIMIT = env_int("NEU_BOX_COMMAND_QUEUE_RECENT", 30)


def _task_public(task: dict) -> dict:
    """任务在统一视图里的对外表示：沿用 ``task_shape.public``，补 ``kind``/``id``。

    老字段一个都不动（master/webui 已经按那套渲染），只多两个字段。
    """
    item = task_shape.public(task)
    item['kind'] = 'task'
    item['id'] = item.get('task_id')
    return item


class TaskQueue(SchedulerMixin):
    """单 Worker 调度队列，任务和 acquire 共用一个异步出队循环。"""

    _instance = None

    def __init__(self):
        self._lock = threading.RLock()
        # 调度回合锁：**一整个调度回合**（探设备池 → 扫队列 → allocate → 摘队 +
        # 置 running）期间不允许任何人改变队列，于是"选中了却在派发前被摘掉"这种
        # 中间状态从构造上不存在 —— `_dispatch` 里那些"分配完再复查、再回滚"的
        # 分支因此可以删掉，取消 pending 也退化成"排队等回合结束再摘条目"。
        #
        # 两条纪律：
        #   1. 锁序固定 ``_round_lock → _lock → SbxManager.lock``，任何持
        #      ``_lock`` 的代码不许反过来拿回合锁；
        #   2. 回合锁内**不许出现没有超时的等待**。dispatch/release 里的 native 调用
        #      都有超时（sandbox helper 30s、docker client 10s），最坏情况是"变更方
        #      多等几十秒"；一旦有人往这条路径塞裸 subprocess 或 Event.wait()，
        #      就会从"慢"变成"整个调度冻死"。
        # 纯读路径（get_queue / position / maintenance_status）不拿回合锁，所以监控
        # 和 /status 不会被派发拖延。
        self._round_lock = threading.Lock()
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
        self._recover_sessions()

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

    def _recover_sessions(self):
        """启动时修一次 acquire 会话账本，并按仍活着的沙盒重建运行中的记账。

        只"修表"、不做恢复：排队中的请求本就是内存态（重启即消失），标成
        ``interrupted`` 只是让账本自洽，不重建请求、不重跑校验。而
        ``state='active'`` 且沙盒**还在**的会话是真活着的 —— 把它装回
        ``_running_acquires``，顺手补掉"重启后运行中的 acquire 记账为空"这个缺口
        （否则 release / pause 的 quiet 判断都看不到它）。
        """
        try:
            closed = self._db.close_interrupted_sessions()
            if closed:
                logger.warning('恢复: 收尾 %s 条没有终态的 acquire 会话', closed)
            for session in self._db.list_active_sessions():
                if session.get('state') != SESSION_ACTIVE:
                    continue
                name = session.get('sandbox_name')
                if not name or self._db.get_sandbox(name) is None:
                    continue
                self._running_acquires[name] = {
                    'request_id': session['request_id'],
                    'owner': session['owner'],
                    'pid': session['pid'],
                    'sandbox_name': name,
                    'devices': list(session.get('devices') or []),
                    'created_at': session.get('requested_at') or 0.0,
                    'acquired_at': session.get('acquired_at') or 0.0,
                }
        except sqlite3.Error:
            logger.exception('恢复: 会话账本对账失败，跳过（不影响本次启动）')

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
        # 入队属于"改变队列"，与调度回合互斥（见 __init__ 的回合锁注释）。
        with self._round_lock, self._lock:
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
        with self._round_lock, self._lock:
            if SbxManager.get_instance().allocations_paused():
                raise SandboxAllocationPaused('Worker 处于暂停维护状态，暂不创建新沙盒')
            # 先落账再入队：账写不进去就直接失败，不留"有请求没账"的状态。
            self._db.insert_session(
                request.request_id, owner, pid,
                device_num=request.device_num, device_ids=request.device_ids,
                priority=request.priority, requested_at=request.created_at,
            )
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

    def _dispatch(self, kind: str, identifier: str):
        """派发一个条目，**同步**跑完；调用方必须持有回合锁。

        `_consume_loop` 在回合锁内调用它，所以"选中的条目在派发前被摘掉""allocate
        期间被取消/暂停"这些窗口都不存在 —— 这里只做"分配 → 摘队 → 置 running"，
        不再需要分配后的复查与回滚。要卡的执行体由 ``asyncio.create_task`` 起在
        协程里，那部分在回合锁之外跑。
        """
        sbx = SbxManager.get_instance()
        if kind == 'acquire':
            with self._lock:
                request = self._lookup('acquire', identifier)
            if request is None:
                return
            allocated = None
            try:
                if request.validator:
                    request.validator()
                self._db.update_session_state(identifier, SESSION_ALLOCATING)
                allocated = resources.allocate(
                    request.owner, str(request.pid), request.cpu, request.mem,
                    request.device_num, request.device_ids or None,
                )
                if allocated is None:
                    # 预判过了、分配时又拿不到（探测到分配之间的窗口）：这一轮
                    # 跳过它，下一轮重扫。**不打退避** —— 队列里的条目原样留着。
                    self._db.update_session_state(identifier, SESSION_QUEUED)
                    return
                if request.device_root:
                    visible = set(devices.discover_nodes(request.device_root))
                    missing = sorted(set(allocated['devices']) - visible)
                    if missing:
                        raise AcquireFailure(
                            f'目标容器没有挂载沙盒设备节点: {missing}',
                            'docker_devices_not_visible',
                        )
                # join 是 native 调用（慢），放在 `_lock` 外面；与 pause 的互斥由
                # 回合锁负责（pause 也要拿回合锁才能改队列）。
                if not sbx.join_sandbox(
                        allocated['sandbox_name'], request.pid, borrowed=True):
                    raise AcquireFailure('加入沙盒失败', 'sandbox_join_failed')
                with self._lock:
                    self._dequeue('acquire', identifier)
                    acquired_at = time.time()
                    self._running_acquires[allocated['sandbox_name']] = {
                        'request_id': identifier,
                        'owner': request.owner,
                        'pid': request.pid,
                        'sandbox_name': allocated['sandbox_name'],
                        'devices': list(allocated.get('devices') or []),
                        'created_at': request.created_at,
                        'acquired_at': acquired_at,
                    }
                    self._acquire_results[identifier] = (allocated, None)
                self._db.update_session_state(
                    identifier, SESSION_ACTIVE,
                    sandbox_name=allocated['sandbox_name'],
                    devices=allocated.get('devices') or [],
                    acquired_at=acquired_at,
                )
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
                self._db.update_session_state(
                    identifier, SESSION_FAILED,
                    code=getattr(exc, 'code', 'sandbox_acquire_failed'),
                    finished_at=time.time(),
                )
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
                task['status'] = 'running'
                task['started_at'] = time.time()
                task['devices'] = allocated['devices']
                task['_executor'] = self._build_executor(
                    task, allocated['sandbox_name'],
                )
                self._dequeue('task', identifier)
                self._running[identifier] = task
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
        if task.get('_canceled'):
            # 取消是独立终态：以前混在 failed 里靠 error 文案区分，统一队列视图
            # 里有 acquire 的 cancelled，任务这边也用同一个词。
            status = 'cancelled'
        else:
            status = ('completed' if result.get('returncode') == 0
                      and not result.get('timed_out') else 'failed')
        finished_at = time.time()
        try:
            self._db.update_task_result(
                task_id, status, result.get('returncode', -1), '', '',
                result.get('timed_out', False), result.get('error'), finished_at,
            )
            self._db.cleanup_old_tasks(keep=MAX_COMPLETED_TASKS)
            self._db.cleanup_old_sessions(keep=MAX_COMPLETED_TASKS)
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
            self._db.update_task_result(
                task_id, 'cancelled' if task.get('_canceled') else 'failed',
                -1, '', '', error=error,
            )
        except Exception as db_exc:
            with self._lock:
                self._maintenance_errors[task_id] = f'保存任务结果失败: {db_exc}'
        with self._lock:
            self._dequeue('task', task_id)
            self._running.pop(task_id, None)

    def delete_tasks(self, task_ids: list[str]) -> int:
        """删除或取消任务（`DELETE /tasks` 的兼容入口）。

        queued / running → 走 :meth:`cancel`（**留痕**：记录保留、终态 ``cancelled``，
        日志保留），跟取消 acquire 会话同一套语义；
        终态（completed/failed/cancelled）→ 仍然真的删记录 + 删日志。
        """
        count = 0
        for task_id in task_ids:
            outcome = self.cancel(entries.KIND_TASK, task_id)
            if outcome['status'] == 'terminal':
                self.delete_task_record(task_id)
            count += 1
        self._wake_scheduler()
        return count

    def delete_task_record(self, task_id: str) -> None:
        """删掉任务记录与日志（终态任务用；排队中/运行中请用 :meth:`cancel`）。"""
        self._db.delete_task(task_id)
        self._remove_log(task_id)

    def cancel(self, kind: str, identifier: str, *,
               host_pid: int | None = None) -> dict:
        """统一取消入口：命令任务和 acquire 会话共用一套"判定 + 动作"。

        返回 ``{'status': ...}``，可能的取值：

          * ``'cancelled'``  —— 排队中的条目被摘出队列（任务留痕为 cancelled，
            会话写 cancelled），不会再有执行体跑起来；
          * ``'cancelling'`` —— 正在运行的任务已发取消信号，终态由执行体落成
            ``cancelled``；
          * ``'released'``   —— 已经拿到卡的 acquire 会话被就地释放（等于 release
            的语义：归还借出的进程、收掉长出来的进程与容器、还卡）；
          * ``'terminal'``   —— 任务已经是终态（调用方决定是否删除记录）；
          * ``'unknown'``    —— 这个 id 既不在队列里、也不在运行中。

        整段在回合锁里跑完（判定与改账在同一临界区），所以不存在"既不在队列、
        也不在 running"的中间态；重复调用天然幂等。

        ``host_pid`` 只有 acquire 用得上：释放时先把调用方自己搬出沙盒 cgroup，
        否则 ``cgroup.kill`` 会把发起释放的那个进程一起带走。
        """
        if kind not in entries.KINDS:
            raise ValueError(f'未知的条目类型: {kind!r}')
        with self._round_lock:
            if kind == entries.KIND_ACQUIRE:
                return self._cancel_acquire_locked(identifier, host_pid)

            with self._lock:
                task = self._lookup('task', identifier)
                running = self._running.get(identifier)
            if task is not None:
                self._dequeue('task', identifier)
                self._db.update_task_result(
                    identifier, 'cancelled', -1, '', '',
                    error='用户手动取消（排队中）',
                )
                return {'status': 'cancelled', 'id': identifier, 'kind': kind}
            if running is not None:
                running['_canceled'] = True
                try:
                    running['_executor'].cancel()
                except Exception:
                    logger.exception('取消任务 %s 失败', identifier)
                return {'status': 'cancelling', 'id': identifier, 'kind': kind}
            return {'status': 'terminal', 'id': identifier, 'kind': kind}

    def _cancel_acquire_locked(self, request_id: str,
                               host_pid: int | None = None) -> dict:
        """取消一个 acquire 会话；调用方必须持有回合锁。"""
        with self._lock:
            pending = self._lookup('acquire', request_id) is not None
            sandbox_name = None
            for name, record in self._running_acquires.items():
                if record.get('request_id') == request_id:
                    sandbox_name = name
                    break

        if pending:
            # 还在排队：直接摘出队列，不再派发。Ctrl-C 之后没人再轮询这个 id，
            # 所以不用（也不该）往 _acquire_results 里留失败结果；账本留痕即可。
            with self._lock:
                self._dequeue('acquire', request_id)
            self._db.update_session_state(
                request_id, SESSION_CANCELLED,
                code='client_cancelled', finished_at=time.time(),
            )
            return {'status': 'cancelled', 'id': request_id,
                    'kind': entries.KIND_ACQUIRE}

        if sandbox_name:
            # 已经拿到卡：在同一个调用里做释放（= release 的语义），不让调用方再补
            # 一次请求。host_pid 是发起方自报的 PID，先把它搬出沙盒 cgroup。
            if host_pid:
                try:
                    SbxManager.get_instance().evacuate_caller(
                        sandbox_name, int(host_pid))
                except Exception:
                    logger.exception(
                        "取消 acquire '%s' 时搬出调用方 %s 失败",
                        sandbox_name, host_pid,
                    )
            ok = self._release_acquire_locked(sandbox_name)
            if ok is False:
                return {'status': 'release_failed', 'id': request_id,
                        'kind': entries.KIND_ACQUIRE,
                        'sandbox_name': sandbox_name}
            return {'status': 'released', 'id': request_id,
                    'kind': entries.KIND_ACQUIRE,
                    'sandbox_name': sandbox_name}

        return {'status': 'unknown', 'id': request_id,
                'kind': entries.KIND_ACQUIRE}

    @staticmethod
    def _remove_log(task_id: str):
        logs.remove(task_id)

    def get_queue(self, kind: str | None = None,
                  state: str | None = None) -> list[dict]:
        """统一队列视图：命令任务 + acquire 会话。

        顺序：先"在跑的"（任务的 running + 会话的 active），再"排队中的"（任务的
        queued + 会话的 queued/allocating，**两类一起**按 :func:`entries.sort_key`
        编号 ``position``/``eta``），最后是最近的终态（两类各取
        ``QUEUE_RECENT_LIMIT`` 条，按收尾时间倒序合并去重）。

        每条都带 ``kind``（``task``/``acquire``）和 ``id``：master 之类的消费者只
        要按 kind 分支渲染就能同时显示两类条目 —— 以前 acquire 根本不在这个列表
        里，排队中的会话甚至没有任何列表接口。
        """
        active_tasks = self._db.get_queue_tasks()
        active_sessions = self._db.list_active_sessions()
        recent_tasks = self._db.get_recent_tasks(limit=QUEUE_RECENT_LIMIT)
        recent_sessions = self._db.list_recent_sessions(limit=QUEUE_RECENT_LIMIT)

        running = [
            (_task_public(task), 'task')
            for task in active_tasks if task['status'] == 'running'
        ]
        running += [
            (session_shape.public(session), 'acquire')
            for session in active_sessions if session['state'] == SESSION_ACTIVE
        ]

        waiting = [
            (task, 'task') for task in active_tasks if task['status'] == 'queued'
        ] + [
            (session, 'acquire') for session in active_sessions
            if session['state'] != SESSION_ACTIVE
        ]
        waiting.sort(key=lambda pair: entries.sort_key(pair[0]))
        eta = 0
        queued: list[tuple[dict, str]] = []
        for position, (value, entry_kind) in enumerate(waiting, 1):
            item = dict(value)
            item['position'] = position
            item['eta'] = eta
            model = _task_public if entry_kind == 'task' else session_shape.public
            queued.append((model(item), entry_kind))
            eta += (value.get('est_time', 0) or 0) if entry_kind == 'task' else 0

        recent: list[tuple[dict, str]] = [
            (_task_public(task), 'task') for task in recent_tasks
        ] + [
            (session_shape.public(session), 'acquire')
            for session in recent_sessions
        ]
        recent.sort(
            key=lambda pair: pair[0].get('finished_at') or 0, reverse=True,
        )
        recent = recent[:QUEUE_RECENT_LIMIT]

        items = running + queued + recent
        if kind:
            items = [pair for pair in items if pair[1] == kind]
        if state:
            items = [
                pair for pair in items if (pair[0].get('status') or '') == state
            ]
        return [item for item, _entry_kind in items]

    def position(self, task_id: str) -> int:
        """条目在"等待中"队列里的位置 —— 两类条目一起编号。

        以前只数任务，master 看到的排队位置会和统一视图对不上；现在统一用
        :func:`entries.sort_key` 在同一条队列上编号。
        """
        with self._lock:
            waiting = [(identifier, value) for (_kind, identifier), value
                       in self._ordered()]
        waiting.sort(key=lambda pair: entries.sort_key(pair[1]))
        for position, (identifier, _value) in enumerate(waiting, 1):
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
        with self._round_lock, self._lock:
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
            self._db.update_session_state(
                request_id, SESSION_CANCELLED,
                code='worker_paused', finished_at=time.time(),
            )
            if not request.result.done():
                request.result.set_exception(error)
        if pending:
            self._db.cleanup_old_sessions(keep=MAX_COMPLETED_TASKS)

    def _reconcile_running_acquires(self):
        """移除已被 sandbox reaper 清理的 acquire 记录，并把账本收尾。

        沙盒不在 `sandboxes` 里说明它已经被收尸回收（终端退出、或者停机维护），
        这种会话不该一直挂在 active 上。
        """
        with self._lock:
            stale = [
                name for name in self._running_acquires
                if self._db.get_sandbox(name) is None
            ]
            for name in stale:
                record = self._running_acquires.pop(name, None)
                request_id = (record or {}).get('request_id')
                if request_id:
                    self._db.update_session_state(
                        request_id, SESSION_RELEASED,
                        code='reaped', finished_at=time.time(),
                    )

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
        # 释放也是"改变队列"（要摘 _running_acquires），而且它和取消 pending 是
        # 同一类操作，统一拿回合锁：判定与摘记录在同一个临界区里，不存在"既不在
        # 队列、也不在 running"的中间态。
        with self._round_lock:
            return self._release_acquire_locked(sandbox_name)

    def _release_acquire_locked(self, sandbox_name: str):
        """:meth:`release_acquire` 的锁内实现；调用方必须持有回合锁。"""
        self._reconcile_running_acquires()
        with self._lock:
            record = self._running_acquires.get(sandbox_name)
        if record is None:
            return None

        ok = SbxManager.get_instance().destroy_sandbox(sandbox_name)
        if ok:
            with self._lock:
                self._running_acquires.pop(sandbox_name, None)
            self._db.update_session_state(
                record['request_id'], SESSION_RELEASED,
                code='released', finished_at=time.time(),
            )
            self._db.cleanup_old_sessions(keep=MAX_COMPLETED_TASKS)
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
