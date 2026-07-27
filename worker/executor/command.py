"""命令执行模块 — 在沙盒中安全执行用户提交的命令。

架构:
  TaskQueue (FIFO)  →  后台线程逐任务消费  →  execute_in_sandbox (SIGSTOP 方案)

API:
  POST /command/run           提交命令（异步），返回 task_id
  GET  /command/queue         查看所有任务队列（公开元数据，不含日志）
  GET  /command/result/<id>   查看自己的任务结果（含日志，需 user_id 匹配）
"""

import logging
import os
import pwd
import signal
import subprocess
import threading
import time
import uuid
from collections import OrderedDict

from flask import Blueprint, request

from executor.sbx_manager import SbxManager

logger = logging.getLogger(__name__)
command_bp = Blueprint('command', __name__)

_raw_timeout = int(os.getenv('command_timeout', '0'))
DEFAULT_TIMEOUT = _raw_timeout if _raw_timeout > 0 else None
MAX_COMPLETED_TASKS = int(os.getenv('command_max_completed', '200'))
QUEUE_RECENT_LIMIT = int(os.getenv('command_queue_recent', '30'))

# 日志文件配置
LOG_DIR = os.getenv('LOG_DIR', os.path.join(os.path.dirname(__file__), '..', 'logs', 'tasks'))


def _remove_log_file(task_id: str):
    """删除任务日志文件（任务被删除时调用）。"""
    log_path = os.path.join(LOG_DIR, f'{task_id}.log')
    try:
        if os.path.isfile(log_path):
            os.remove(log_path)
            logger.info("已删除日志文件 %s", log_path)
    except Exception as e:
        logger.warning("删除日志文件失败 %s: %s", log_path, e)


# ==================================================================
# 安全执行 — preexec_fn 写 cgroup.procs + setuid 切用户
# ==================================================================
#
# 流程:
#   建沙盒 → Popen(preexec_fn: 写 cgroup.procs → setuid → exec bash)
#   → 更新 DB → communicate(等待完成) → 销毁沙盒
#
# preexec_fn 运行在 fork 之后、exec 之前的子进程中：
#   1. 此时仍是 root，可以写 cgroup.procs
#   2. setgid/setuid 切到目标用户
#   3. chdir 到用户 HOME
#   4. 退出 preexec_fn，子进程 exec bash -c <命令>
#
# 为什么不用 SIGSTOP:
#   之前的 SIGSTOP 方案是 preexec_fn + os.kill(getpid(), SIGSTOP)，
#   在 ARM64 Python 上子进程直接退出。write() + setuid() 不涉及
#   信号操作，不受该问题影响。
# ==================================================================


def _cgroup_procs_path(sandbox_name: str) -> str:
    return f"/sys/fs/cgroup/sandbox_{sandbox_name}/cgroup.procs"


def execute_in_sandbox(
    command: str,
    sandbox_name: str,
    cpu: int = 0,
    mem: str = "0",
    devices: list = None,
    timeout: int | None = DEFAULT_TIMEOUT,
    username: str = '',
) -> dict:
    """在沙盒中以指定用户身份安全执行一条命令。

    stderr 已在 shell 层 2>&1 合并到 stdout，保证输出按时间序排列。

    流程:
      1. 建沙盒（空壳）
      2. Popen(preexec_fn: 写 PID 到 cgroup.procs → setuid 切用户 → 返回)
      3. 更新 DB 记录（join_sandbox 做幂等确认）
      4. 启动线程逐行读取 stdout，增量写入 DB（前端刷新可拉到部分日志）
      5. proc.wait(timeout) 等待进程结束
      6. 清理沙盒，返回完整结果
    """
    sbx = SbxManager.get_instance()

    # 1. 建沙盒（空壳，还没有进程）
    ok = sbx.create_sandbox(sandbox_name, cpu=cpu, mem=mem,
                            devices=devices if devices else None)
    if not ok:
        logger.error("沙盒 '%s' 创建失败", sandbox_name)
        return {
            'returncode': -1, 'stdout': '', 'stderr': f'Failed to create sandbox "{sandbox_name}"',
            'timed_out': False, 'error': 'sandbox_create_failed',
        }

    proc = None
    cg_procs = _cgroup_procs_path(sandbox_name)

    # 2. 构建 preexec_fn：写 cgroup.procs + 切用户
    if username:
        try:
            pw = pwd.getpwnam(username)
            target_uid = pw.pw_uid
            target_gid = pw.pw_gid
            target_dir = pw.pw_dir
        except KeyError:
            return {
                'returncode': -1, 'stdout': '', 'stderr': f'Unknown user: {username}',
                'timed_out': False, 'error': 'unknown_user',
            }

        def preexec():
            # 1) 写 PID 到 cgroup（仍是 root）
            with open(cg_procs, 'w') as f:
                f.write(str(os.getpid()))
            # 2) 初始化附加组（docker 等），然后切到目标用户
            os.initgroups(username, target_gid)
            os.setgid(target_gid)
            os.setuid(target_uid)
            os.chdir(target_dir)
            os.environ['HOME'] = target_dir
    else:
        # 不切用户，仅写 cgroup
        def preexec():
            with open(cg_procs, 'w') as f:
                f.write(str(os.getpid()))

    try:
        logger.warning("启动进程, cgroup=%s, user=%s", cg_procs, username or '(root)')
        # bash -i 交互模式：自动 source ~/.bashrc 完整内容（绕过开头的 *i* guard）
        # exec 2>&1 把 bash 的 stderr 全局合并到 stdout
        full_command = f'exec 2>&1; {command}'
        # PYTHONUNBUFFERED=1 强制 Python 子进程行缓冲输出
        # bufsize=1 确保 Python 端管道行缓冲，数据即到即读
        # 注意: 传 env dict 时 execve 会绕过 preexec_fn 的 os.environ 修改，
        # 因此 HOME 必须在 dict 中显式设置为目标用户的 home 目录
        popen_env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        if username:
            popen_env['HOME'] = target_dir
        proc = subprocess.Popen(
            ['bash', '-i', '-c', full_command],
            preexec_fn=preexec,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,  # 无缓冲，read() 即到即返
            env=popen_env,
        )
        logger.warning("子进程 PID=%s 已启动", proc.pid)

        # 3. 更新 DB 记录（join_sandbox 也会写 cgroup.procs，幂等）
        time.sleep(0.05)
        try:
            sbx.join_sandbox(sandbox_name, proc.pid)
        except Exception as e:
            logger.warning("join_sandbox 跳过 (进程可能已退出): %s", e)

        # 4. 边跑边写日志：启动线程读取 stdout，写入文件（非 DB）
        # 沙盒命名格式: sbx_{owner}_{id}.slice
        if sandbox_name.startswith('sbx_'):
            parts = sandbox_name[:-6].split('_', 2)
            task_id = parts[2] if len(parts) > 2 else sandbox_name
        else:
            task_id = sandbox_name

        stdout_lines = []
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f'{task_id}.log')

        def _read_stdout():
            try:
                with open(log_path, 'a') as f:
                    while True:
                        chunk = proc.stdout.read(4096)
                        if not chunk:
                            break
                        text = chunk.decode('utf-8', errors='replace')
                        stdout_lines.append(text)
                        f.write(text)
                        f.flush()
            except Exception as e:
                logger.warning("读取 stdout 流异常: %s", e)

        t = threading.Thread(target=_read_stdout, daemon=True)
        t.start()

        # 5. 等待进程结束（带超时）
        logger.warning("等待 PID=%s 完成 (timeout=%ss)", proc.pid, timeout)
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning("PID=%s 超时，正在终止...", proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass

        # 等待读取线程结束（进程退出后 readline 收到 EOF 自动退出）
        t.join(timeout=5)

        logger.warning("PID=%s 完成, rc=%s", proc.pid, proc.returncode)
        return {
            'returncode': proc.returncode if not timed_out else -1,
            'stdout': ''.join(stdout_lines),
            'stderr': '',
            'timed_out': timed_out,
            'error': None,
        }

    except Exception as e:
        logger.error("异常: %s: %s", type(e).__name__, e, exc_info=True)
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        return {
            'returncode': -1, 'stdout': '', 'stderr': f'Execution error: {e}',
            'timed_out': False, 'error': 'exception',
        }
    finally:
        logger.warning("清理沙盒 '%s'", sandbox_name)
        try:
            sbx.destroy_sandbox(sandbox_name)
        except Exception as e:
            logger.error("清理沙盒异常: %s", e)


# ==================================================================
# 任务队列（单例，FIFO）
# ==================================================================

from executor.db import Database


class TaskQueue:
    """FIFO 任务队列管理器（单例）。

    每个 Worker 有自己的队列，后台线程逐任务消费。
    任务数据持久化到 SQLite，进程重启后可恢复历史记录。
    """

    _instance = None

    def __init__(self):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

        # 等待队列: 有序字典保持 FIFO，key=task_id（内存，驱动消费线程）
        self._pending: OrderedDict[str, dict] = OrderedDict()
        # 当前正在执行的任务（支持并发）
        self._running: dict[str, dict] = {}
        self._device_lock = threading.Lock()  # 设备分配互斥锁

        self._running_flag = False
        self._worker_thread: threading.Thread | None = None

        # 数据库实例
        self._db = Database.get_instance()

        # 启动时恢复：标记上次异常退出的 running 任务为 failed
        self._recover_orphaned()

    @classmethod
    def get_instance(cls) -> 'TaskQueue':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 启动/停止 ─────────────────────────────────────────────

    def start(self):
        if self._running_flag:
            return
        self._running_flag = True
        self._worker_thread = threading.Thread(
            target=self._consume_loop, daemon=True, name='task-queue-consumer')
        self._worker_thread.start()
        logger.info('后台消费线程已启动')

    def _recover_orphaned(self):
        """启动恢复：将上次异常退出的任务复原。"""
        all_active = self._db.get_queue_tasks()
        for task in all_active:
            if task['status'] == 'running':
                logger.warning('恢复: 标记孤儿任务 %s 为 failed', task['task_id'])
                self._db.update_task_result(
                    task['task_id'], 'failed', -1, '', '',
                    error='Worker 可能在执行过程中重启')
            elif task['status'] == 'queued':
                # 重新加入内存队列（按原 position 排序）
                logger.info('恢复: 重新入队 %s (原 position=%s)',
                            task['task_id'], task.get('position'))
                self._pending[task['task_id']] = {
                    'task_id': task['task_id'],
                    'user_id': task['user_id'],
                    'command': task['command'],
                    'cpu': task.get('cpu', 0),
                    'mem': task.get('mem', '0'),
                    'device_num': len(task.get('devices') or []),
                    'devices': [],
                    'status': 'queued',
                    'position': 0,
                    'created_at': task.get('created_at', time.time()),
                    'started_at': None,
                    'finished_at': None,
                    'result': None,
                }
        # 重新排位
        if self._pending:
            self._reindex()
            logger.info('恢复完成: %s 个任务重新入队', len(self._pending))

    # ── 提交 ──────────────────────────────────────────────────

    def submit(self, user_id: str, command: str,
               cpu: int = 0, mem: str = "0",
               device_num: int = 0,
               device_ids: list = None) -> str:
        """提交任务到队列。device_ids 指定设备时优先于 device_num 自动分配。"""
        task_id = uuid.uuid4().hex[:12]

        self._db.insert_task(
            task_id=task_id, user_id=user_id, command=command,
            cpu=cpu, mem=mem, devices=[])

        task = {
            'task_id': task_id,
            'user_id': user_id,
            'command': command,
            'cpu': cpu,
            'mem': mem,
            'device_num': device_num,
            'device_ids': device_ids or [],   # 用户指定的设备列表
            'devices': [],          # 执行时才分配
            'status': 'queued',
            'position': 0,
            'created_at': time.time(),
            'started_at': None,
            'finished_at': None,
            'result': None,
        }

        with self._lock:
            self._pending[task_id] = task
            self._reindex()
            self._cv.notify()

        logger.info('任务入队: %s user=%s cmd=%s...', task_id, user_id, command[:60])
        return task_id

    # ── 查询 ──────────────────────────────────────────────────

    def get_queue(self) -> list[dict]:
        """返回队列视图：运行中 → 排队中 → 最近完成/失败的任务（不含日志）。
        从 DB 读取，保证进程重启后数据不丢。
        """
        active = self._db.get_queue_tasks()              # queued + running
        recent = self._db.get_recent_tasks(limit=QUEUE_RECENT_LIMIT)  # completed + failed
        # 去重：active 中的 task_id 优先（理论上状态不同不会重复）
        active_ids = {t['task_id'] for t in active}
        all_tasks = active + [t for t in recent if t['task_id'] not in active_ids]
        return [self._format_public(t) for t in all_tasks]

    def delete_tasks(self, task_ids: list[str]) -> int:
        """批量删除。排队任务直接移除，运行中任务设置取消标记 + 杀沙盒。
        I/O 在锁外执行，避免死锁。"""
        to_kill: list[str] = []       # running → 需要杀沙盒
        to_delete_db: list[str] = []  # 历史任务 → 直接删 DB

        with self._lock:
            for tid in task_ids:
                if tid in self._pending:
                    del self._pending[tid]
                    to_delete_db.append(tid)
                elif tid in self._running:
                    self._running[tid]['_canceled'] = True
                    del self._running[tid]
                    to_kill.append(tid)
                else:
                    to_delete_db.append(tid)
            if to_delete_db or tid in self._pending:  # re-check: just always reindex if any change
                pass
            self._reindex()

        # 锁外：删 DB + 杀沙盒 + 清理日志文件
        deleted = 0
        for tid in to_delete_db:
            self._db.delete_task(tid)
            _remove_log_file(tid)
            deleted += 1

        sbx = SbxManager.get_instance()
        for tid in to_kill:
            try:
                # 从 DB 查沙盒名称
                task = self._db.get_task(tid)
                user_id = (task.get('user_id') or 'unknown') if task else 'unknown'
                sbx.destroy_sandbox(f"sbx_{user_id}_{tid}.slice")
            except Exception as e:
                logger.warning('销毁沙盒 %s 失败: %s', tid, e)
            self._db.update_task_result(
                tid, 'failed', -1, '', '', error='用户手动取消')
            # 保留日志文件，用户再次删除时才会清理
            deleted += 1
            logger.info('已取消运行中任务 %s (日志保留)', tid)

        if deleted > 0:
            logger.info('批量删除 %s 个任务', deleted)
        return deleted

    def get_result(self, task_id: str) -> dict | None:
        """获取任务结果元数据（不含日志内容，日志走 /log 接口）。"""
        task = self._db.get_task(task_id)
        if task is None:
            return None

        public = self._format_public(task)
        public['result'] = {
            'returncode': task.get('returncode'),
            'timed_out': bool(task.get('timed_out')),
        }
        return public

    # ── 内部 ──────────────────────────────────────────────────

    def _reindex(self):
        """更新所有 pending 任务的 position 字段（FIFO 顺序）。"""
        task_ids = list(self._pending.keys())
        for i, tid in enumerate(task_ids):
            self._pending[tid]['position'] = i + 1
        # 同步到 DB
        if task_ids:
            self._db.update_position_batch(task_ids)

    @staticmethod
    def _format_public(task: dict) -> dict:
        """返回任务的公开视图（不含日志）。"""
        return {
            'task_id': task['task_id'],
            'user_id': task['user_id'],
            'command': task['command'],
            'status': task['status'],
            'position': task.get('position', 0),
            'cpu': task.get('cpu', 0),
            'mem': task.get('mem', '0'),
            'device_num': task.get('device_num', len(task.get('devices') or [])),
            'devices': task.get('devices', []),
            'created_at': task.get('created_at'),
            'started_at': task.get('started_at'),
            'finished_at': task.get('finished_at'),
        }

    # ── 消费循环 ──────────────────────────────────────────────

    def _execute_one(self, task: dict, devices: list):
        """在独立线程中执行单个任务。若执行中被取消则跳过写 DB（避免覆盖取消状态）。"""
        sandbox_name = f"sbx_{task['user_id']}_{task['task_id']}.slice"
        try:
            result = execute_in_sandbox(
                command=task['command'],
                sandbox_name=sandbox_name,
                cpu=task.get('cpu', 0),
                mem=task.get('mem', '0'),
                devices=devices if devices else None,
                username=task['user_id'],
            )

            # 执行中被取消 → 不覆盖 DB
            if task.get('_canceled'):
                logger.info('任务 %s 已被用户取消，跳过写 DB', task['task_id'])
                return

            finished_at = time.time()
            status = ('completed' if result.get('returncode') == 0
                      and not result.get('timed_out') else 'failed')
            # 日志已存入文件，DB 只保留元数据
            self._db.update_task_result(
                task_id=task['task_id'],
                status=status,
                returncode=result.get('returncode', -1),
                stdout='', stderr='',
                timed_out=result.get('timed_out', False),
                error=result.get('error'),
                finished_at=finished_at,
            )
            self._db.cleanup_old_tasks(keep=MAX_COMPLETED_TASKS)

            with self._lock:
                task['result'] = result
                task['finished_at'] = finished_at
                task['status'] = status
                if task['task_id'] in self._running:
                    del self._running[task['task_id']]
                self._cv.notify()

            logger.info('执行完成: %s status=%s returncode=%s',
                        task['task_id'], status, result.get('returncode'))
        except Exception as e:
            logger.error('任务 %s 异常: %s', task['task_id'], e)
            with self._lock:
                if task['task_id'] in self._running:
                    del self._running[task['task_id']]
                self._cv.notify()

    def _consume_loop(self):
        """后台线程：先探测设备再取任务，避免取出又放回的死循环。
        锁顺序：_lock → _device_lock（只在 peek 阶段，不嵌套）。"""
        sbx = SbxManager.get_instance()
        while self._running_flag:
            task = None
            try:
                # ── 1. peek 队首，检查设备 ──
                device_num = 0
                with self._lock:
                    if not self._pending:
                        self._cv.wait(timeout=5)
                        continue
                    first_tid = next(iter(self._pending))
                    device_num = self._pending[first_tid].get('device_num', 0)

                # 优先使用用户指定的 device_ids，其次自动分配
                device_ids = self._pending[first_tid].get('device_ids', []) or []
                allocated = []
                if device_ids:
                    with self._device_lock:
                        free = sbx._get_free_devices()
                        unavailable = [d for d in device_ids if d not in free]
                        if unavailable:
                            logger.warning('指定设备 %s 不可用，等待中...', unavailable)
                            time.sleep(3)
                            continue
                        allocated = list(device_ids)
                elif device_num > 0:
                    with self._device_lock:
                        free = sbx._get_free_devices()
                        if len(free) < device_num:
                            time.sleep(3)
                            continue
                        allocated = free[:device_num]

                # ── 2. 设备满足，正式出队 ──
                with self._lock:
                    if not self._pending:
                        continue
                    _, task = self._pending.popitem(last=False)
                    task['status'] = 'running'
                    task['started_at'] = time.time()
                    task['devices'] = allocated
                    self._running[task['task_id']] = task
                    self._reindex()

                logger.warning('任务 %s 分配设备: %s', task['task_id'], allocated)

                # ── 3. 锁外：建沙盒（锁定设备）→ 更新 DB → 启动线程 ──
                if allocated:
                    ok = sbx.create_sandbox(
                        f"sbx_{task['user_id']}_{task['task_id']}.slice",
                        cpu=task.get('cpu', 0),
                        mem=task.get('mem', '0'),
                        devices=allocated)
                    if not ok:
                        logger.error('任务 %s 沙盒创建失败', task['task_id'])
                        with self._lock:
                            if task['task_id'] in self._running:
                                del self._running[task['task_id']]
                            self._cv.notify()
                        continue

                self._db.update_task_status(
                    task['task_id'], 'running', started_at=task['started_at'],
                    devices=allocated if allocated else None)

                logger.info('开始执行: %s user=%s cmd=%s... devices=%s',
                            task['task_id'], task['user_id'],
                            task['command'][:60], allocated)
                t = threading.Thread(
                    target=self._execute_one,
                    args=(task, allocated),
                    daemon=True,
                    name=f'cmd-{task["task_id"][:8]}',
                )
                t.start()

            except Exception:
                logger.exception('消费循环异常，task=%s', task['task_id'] if task else 'None')
                with self._lock:
                    if task and task['task_id'] in self._running:
                        del self._running[task['task_id']]
                    self._cv.notify()
                time.sleep(1)


# ==================================================================
# Flask 路由
# ==================================================================

@command_bp.route('/run', methods=['POST'])
def run_command():
    """提交命令到任务队列（异步）。

    请求体 (JSON):
        { "command": "...", "user_id": "...", "cpu": 2, "memory": 4,
          "mem_unit": "GB", "device_num": 1 }

    响应: { "task_id": "abc123", "position": 3, "message": "..." }
    """
    body = request.get_json(silent=True) or {}

    command = (body.get('command') or '').strip()
    if not command:
        return {'error': '命令不能为空'}, 400

    user_id = (body.get('user_id') or '').strip()
    if not user_id:
        return {'error': 'user_id 不能为空'}, 400

    cpu = body.get('cpu', 0)
    if not isinstance(cpu, int) or cpu < 0:
        cpu = 0

    mem_val = body.get('memory', 0)
    mem_unit = body.get('mem_unit', 'GB')
    if not isinstance(mem_val, int) or mem_val < 0:
        mem_val = 0
    if mem_val == 0:
        sandbox_mem = '0'
    elif mem_unit == 'GB':
        sandbox_mem = f'{mem_val}G'
    else:
        sandbox_mem = f'{mem_val}M'

    device_num = body.get('device_num', 0)
    device_ids = body.get('device_ids')  # 可选: ["1","3"] 或 ["235:1","235:3"]
    if not isinstance(device_num, int) or device_num < 0:
        device_num = 0

    # 防呆：请求数量超过系统设备总数，直接拒绝
    sbx = SbxManager.get_instance()
    if device_num > 0:
        total = len(sbx._discover_device_nodes())
        if device_num > total:
            return {'error': f'设备不足: 需要 {device_num} 个, 系统共 {total} 个'}, 400

    # 归一化 device_ids
    normalized_ids = None
    if device_ids and isinstance(device_ids, list):
        normalized_ids = _normalize_device_ids(device_ids, sbx._discover_device_nodes())

    tq = TaskQueue.get_instance()
    task_id = tq.submit(
        user_id=user_id,
        command=command,
        cpu=cpu,
        mem=sandbox_mem,
        device_num=device_num,
        device_ids=normalized_ids,
    )

    # 从 DB 获取当前队列位置
    db = Database.get_instance()
    task = db.get_task(task_id)
    position = task['position'] if task else 0

    return {
        'task_id': task_id,
        'position': position,
        'message': f'任务已提交，队列位置 #{position}',
    }, 202


@command_bp.route('/tasks/delete', methods=['POST'])
def delete_tasks():
    """批量删除任务。正在运行的任务不会被删除。

    Body: { "task_ids": ["id1", "id2", ...] }
    响应: { "deleted": N }
    """
    data = request.get_json(silent=True) or {}
    task_ids = data.get('task_ids') or []
    if not task_ids:
        return {'error': 'task_ids 不能为空'}, 400

    tq = TaskQueue.get_instance()
    deleted = tq.delete_tasks(task_ids)
    return {'deleted': deleted, 'message': f'已删除 {deleted} 个任务'}, 200


@command_bp.route('/queue', methods=['GET'])
def get_queue():
    """查看当前任务队列（所有用户的待执行 + 正在执行的任务，不含日志）。

    响应: { "queue": [...], "running": {...} }
    """
    tq = TaskQueue.get_instance()
    return {
        'queue': tq.get_queue(),
        'total_pending': len(tq._pending),
    }, 200


@command_bp.route('/result/<task_id>', methods=['GET'])
def get_result(task_id: str):
    """查看任务结果元数据（状态、返回码等，不含日志内容）。"""
    tq = TaskQueue.get_instance()
    result = tq.get_result(task_id)
    if result is None:
        return {'error': '任务不存在'}, 404
    return result, 200


@command_bp.route('/result/<task_id>/log', methods=['GET'])
def get_result_log(task_id: str):
    """获取任务日志文件内容。

    Query params:
        raw=1        返回纯文本 + Content-Length（前端进度条用）
        tail=N        返回文件末尾 N 字节
        offset=N&limit=M  返回从 offset 开始的 M 字节（默认 16KB）

    默认 JSON: { "data": "<text>", "offset": N, "total_size": N }
    raw 模式:  纯文本响应，带 Content-Length 头
    """
    log_path = os.path.join(LOG_DIR, f'{task_id}.log')
    if not os.path.isfile(log_path):
        if request.args.get('raw'):
            return '', 200  # flask 自动 text/plain
        return {'data': '', 'offset': 0, 'total_size': 0}, 200

    file_size = os.path.getsize(log_path)
    tail = _parse_int(request.args.get('tail'), 0)
    offset = _parse_int(request.args.get('offset'), 0)
    limit = _parse_int(request.args.get('limit'), 0)
    raw_mode = request.args.get('raw')

    if tail and tail > 0:
        offset = max(0, file_size - tail)
        limit = min(tail, file_size)
    elif not limit and not offset:
        # 没指定任何范围 → 全量返回
        limit = file_size

    offset = max(0, min(offset, file_size))
    limit = max(1, min(limit, file_size - offset))

    try:
        with open(log_path, 'rb') as f:
            f.seek(offset)
            raw = f.read(limit)
    except Exception as e:
        logger.warning("读取日志文件失败 %s: %s", log_path, e)
        return {'data': '', 'offset': 0, 'total_size': file_size, 'error': str(e)}, 500

    if raw_mode:
        text = raw.decode('utf-8', errors='replace')
        return text, 200, {'Content-Type': 'text/plain; charset=utf-8'}

    data = raw.decode('utf-8', errors='replace')
    return {
        'data': data,
        'offset': offset,
        'total_size': file_size,
    }, 200


def _parse_int(value: str | None, default: int) -> int:
    """安全解析整型 query param，解析失败返回默认值。"""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _normalize_device_ids(raw: list, all_devices: list[str]) -> list[str] | None:
    """将用户指定设备 ID 归一化: 纯数字→匹配 minor 号, "m:M"→原样。"""
    result = []
    for d in raw:
        d = str(d).strip()
        if not d:
            continue
        if ':' in d:
            result.append(d)
        else:
            for dev in all_devices:
                if dev.endswith(f':{d}'):
                    result.append(dev)
                    break
    return result if result else None


# ── 启动 TaskQueue（在 import 时自动启动） ──
TaskQueue.get_instance().start()
