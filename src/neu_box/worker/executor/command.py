"""命令执行模块 — 在沙盒中安全执行用户提交的命令。

架构:
  TaskQueue (FIFO)  →  sandbox 设备分配  →  Host / Docker Exec

API:
  POST   /tasks              提交命令（异步），返回 task_id
  GET    /tasks              查看任务队列（公开元数据，不含日志）
  GET    /tasks/<id>         查看任务结果
  GET    /tasks/<id>/log     增量读取任务日志
  DELETE /tasks              批量删除或取消任务
"""

import logging
import os
import pwd
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import OrderedDict

from flask import Blueprint, request

from neu_box.config import env_int
from neu_box.worker.executor.sbx_manager import SbxManager
from neu_box.worker.executor.command_docker import ExistingDockerCommandExecutor
from neu_box.worker.executor.command_target import (
    TARGET_DOCKER_EXISTING,
    TARGET_HOST,
    TargetValidationError,
    normalize_execution_target,
    public_execution_target,
)
from neu_box.worker.paths import task_logs_dir

logger = logging.getLogger(__name__)
command_bp = Blueprint('command', __name__)

_raw_timeout = env_int("NEU_BOX_COMMAND_TIMEOUT", 0)
DEFAULT_TIMEOUT = _raw_timeout if _raw_timeout > 0 else None
MAX_COMPLETED_TASKS = env_int("NEU_BOX_COMMAND_MAX_COMPLETED", 200)
QUEUE_RECENT_LIMIT = env_int("NEU_BOX_COMMAND_QUEUE_RECENT", 30)

# 日志文件配置
LOG_DIR = str(task_logs_dir())


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


def _normalize_device_ids(raw: list, all_devices: list[str]) -> list[str] | None:
    by_minor = {device.split(':', 1)[1]: device for device in all_devices}
    result = []
    for value in raw:
        value = str(value).strip()
        device = value if value in all_devices else by_minor.get(value)
        if device is None:
            return None
        if device not in result:
            result.append(device)
    return result or None


def execute_in_sandbox(
    command: str,
    sandbox_name: str,
    timeout: int | None = DEFAULT_TIMEOUT,
    username: str = '',
) -> dict:
    """在沙盒中以指定用户身份安全执行一条命令。

    stderr 在 Popen 层合并到 stdout，保证 Bash 启动、语法解析和命令运行
    各阶段的错误都按时间顺序进入同一个任务日志。

    流程:
      1. TaskQueue 已经选择设备并创建沙盒
      2. Popen(preexec_fn: 写 PID 到 cgroup.procs → setuid 切用户 → 返回)
      3. 更新 DB 记录（join_sandbox 做幂等确认）
      4. 启动线程逐行读取 stdout，增量写入日志文件
      5. proc.wait(timeout) 等待进程结束

    sandbox 由 TaskQueue 在所有执行目标的统一 finally 路径中销毁。
    """
    sbx = SbxManager.get_instance()

    target_uid = target_gid = None
    target_dir = None
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

    proc = None
    cg_procs = _cgroup_procs_path(sandbox_name)

    # 2. 构建 preexec_fn：写 cgroup.procs + 切用户
    if username:
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
        # bash -i 交互模式：自动 source ~/.bashrc 完整内容（绕过开头的 *i* guard）。
        # stderr 必须在 Popen 层合并：若用户命令有 Bash 语法错误，解析会发生
        # 在命令字符串中的 `exec 2>&1` 执行之前，单独的 stderr pipe 会丢失报错。
        # PYTHONUNBUFFERED=1 强制 Python 子进程行缓冲输出
        # bufsize=1 确保 Python 端管道行缓冲，数据即到即读
        # 注意: 传 env dict 时 execve 会绕过 preexec_fn 的 os.environ 修改，
        # 因此 HOME 必须在 dict 中显式设置为目标用户的 home 目录
        popen_env = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        if username:
            popen_env['HOME'] = target_dir
        proc = subprocess.Popen(
            ['bash', '-i', '-c', command],
            preexec_fn=preexec,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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


class HostCommandExecutor:
    """保持原有 Host command 行为的 Executor 适配器。"""

    def __init__(
        self,
        *,
        task: dict,
        sandbox_name: str,
    ):
        self.task = task
        self.sandbox_name = sandbox_name

    def run(self, timeout: int | None) -> dict:
        return execute_in_sandbox(
            command=self.task['command'],
            sandbox_name=self.sandbox_name,
            timeout=timeout,
            username=self.task['user_id'],
        )

    def cancel(self):
        # cgroup.kill 同时覆盖主进程及其后台子进程。
        SbxManager.get_instance().destroy_sandbox(self.sandbox_name)


# ==================================================================
# 任务队列（单例，FIFO）
# ==================================================================

from neu_box.worker.executor.db import Database



class TaskQueue:
    """每个 Worker 一个命令队列。

    单队列，出队顺序为 优先级 DESC → created_at ASC（FIFO）：
    priority 数字越大越先执行（当前 0=普通，1=赶论文），
    同优先级内按提交时间先到先执行。
    """

    _instance = None

    def __init__(self):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending: OrderedDict[str, dict] = OrderedDict()
        self._running: dict[str, dict] = {}
        self._running_flag = False
        self._worker_thread: threading.Thread | None = None
        self._db = Database.get_instance()
        self._recover_orphaned()

    @classmethod
    def get_instance(cls) -> 'TaskQueue':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _sandbox_name(task: dict) -> str:
        return f"sbx_{task['user_id']}_{task['task_id']}.slice"

    def start(self):
        if self._running_flag:
            return
        self._running_flag = True
        self._worker_thread = threading.Thread(
            target=self._consume_loop,
            daemon=True,
            name='task-queue-consumer',
        )
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
                    'device_num': task.get('device_num', 0),
                    'device_ids': task.get('device_ids') or [],
                    'target': task.get('target_spec') or {
                        'type': TARGET_HOST,
                    },
                    'est_time': task.get('est_time', 0),
                    'priority': task.get('priority', 0) or 0,
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

    def submit(
        self,
        user_id: str,
        command: str,
        cpu: int = 0,
        mem: str = '0',
        device_num: int = 0,
        device_ids: list | None = None,
        target: dict | None = None,
        est_time: int = 0,
        priority: int = 0,
    ) -> str:
        """提交任务到队列。device_ids 指定设备时优先于 device_num 自动分配。"""
        task_id = uuid.uuid4().hex[:12]
        target = dict(target or {'type': TARGET_HOST})
        device_ids = list(device_ids or [])
        self._db.insert_task(
            task_id=task_id,
            user_id=user_id,
            command=command,
            cpu=cpu,
            mem=mem,
            devices=[],
            device_num=device_num,
            device_ids=device_ids,
            target=target,
            est_time=est_time,
            priority=priority,
        )
        task = {
            'task_id': task_id,
            'user_id': user_id,
            'command': command,
            'cpu': cpu,
            'mem': mem,
            'device_num': device_num,
            'device_ids': device_ids,
            'target': target,
            'est_time': est_time,
            'priority': priority,
            'devices': [],
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
        logger.info(
            '任务入队: %s user=%s priority=%s cmd=%s...',
            task_id,
            user_id,
            priority,
            command[:60],
        )
        return task_id

    def get_queue(self) -> list[dict]:
        """返回队列视图：运行中 → 排队中 → 最近完成/失败的任务（不含日志）。
        计算每个排队任务的预估等待时间（前面排队任务 est_time 之和）。
        """
        active = self._db.get_queue_tasks()              # queued + running
        recent = self._db.get_recent_tasks(limit=QUEUE_RECENT_LIMIT)

        # 计算每个排队任务的 eta：前面所有排队任务的 est_time 累加
        queued_tasks = [t for t in active if t['status'] == 'queued']
        queued_tasks.sort(key=lambda t: t.get('position', 0))
        eta_sum = 0
        eta_map = {}
        for t in queued_tasks:
            eta_map[t['task_id']] = eta_sum
            eta_sum += t.get('est_time', 0) or 0

        active_ids = {t['task_id'] for t in active}
        all_tasks = active + [t for t in recent if t['task_id'] not in active_ids]
        return [self._format_public(t, eta=eta_map.get(t['task_id']))
                for t in all_tasks]

    def delete_tasks(self, task_ids: list[str]) -> int:
        to_cancel = []
        to_delete = []
        with self._lock:
            for task_id in task_ids:
                if task_id in self._pending:
                    del self._pending[task_id]
                    to_delete.append(task_id)
                elif task_id in self._running:
                    task = self._running[task_id]
                    task['_canceled'] = True
                    to_cancel.append(task)
                else:
                    to_delete.append(task_id)
            self._reindex()

        deleted = 0
        for task_id in to_delete:
            self._db.delete_task(task_id)
            _remove_log_file(task_id)
            deleted += 1

        sbx = SbxManager.get_instance()
        for task in to_cancel:
            try:
                task['_executor'].cancel()
            except Exception:
                logger.exception('取消执行器 %s 失败', task['task_id'])
            try:
                sbx.destroy_sandbox(self._sandbox_name(task))
            except Exception:
                logger.exception('销毁 sandbox %s 失败', task['task_id'])
            deleted += 1
        return deleted

    def get_result(self, task_id: str) -> dict | None:
        task = self._db.get_task(task_id)
        if task is None:
            return None
        public = self._format_public(task)
        public['result'] = {
            'returncode': task.get('returncode'),
            'timed_out': bool(task.get('timed_out')),
            'error': task.get('error'),
        }
        return public

    @staticmethod
    def _sort_key(task: dict):
        """队列排序键：优先级 DESC，同级按创建时间 ASC（FIFO），task_id 兜底。"""
        return (
            -(task.get('priority', 0) or 0),
            task.get('created_at') or 0.0,
            task['task_id'],
        )

    def _head_task_id(self) -> str | None:
        """返回当前队首 task_id（持锁调用）。空队列返回 None。"""
        if not self._pending:
            return None
        return min(self._pending, key=lambda tid: self._sort_key(self._pending[tid]))

    def _reindex(self):
        task_ids = sorted(
            self._pending, key=lambda tid: self._sort_key(self._pending[tid])
        )
        for position, task_id in enumerate(task_ids, start=1):
            self._pending[task_id]['position'] = position
        if task_ids:
            self._db.update_position_batch(task_ids)

    @staticmethod
    def _format_public(task: dict, eta: int = None) -> dict:
        """返回任务的公开视图（不含日志）。"""
        target = task.get('target')
        if target is None:
            target = task.get('target_spec')
        return {
            'task_id': task['task_id'],
            'user_id': task['user_id'],
            'command': task['command'],
            'status': task['status'],
            'position': task.get('position', 0),
            'priority': task.get('priority', 0) or 0,
            'cpu': task.get('cpu', 0),
            'est_time': task.get('est_time', 0) or 0,
            'eta': eta,  # 预估等待时间（分钟），仅 queued 任务有值
            'mem': task.get('mem', '0'),
            'device_num': task.get(
                'device_num',
                len(task.get('devices') or []),
            ),
            'devices': task.get('devices', []),
            'target': public_execution_target(target),
            'created_at': task.get('created_at'),
            'started_at': task.get('started_at'),
            'finished_at': task.get('finished_at'),
        }

    @staticmethod
    def _build_executor(task: dict, sandbox_name: str, devices: list):
        target_type = task['target'].get('type', TARGET_HOST)
        if target_type == TARGET_HOST:
            return HostCommandExecutor(
                task=task,
                sandbox_name=sandbox_name,
            )
        return ExistingDockerCommandExecutor(
            task=task,
            sandbox_name=sandbox_name,
            devices=devices,
            log_path=os.path.join(
                LOG_DIR,
                f'{task["task_id"]}.log',
            ),
        )

    def _execute_one(self, task: dict):
        sandbox_name = self._sandbox_name(task)
        result = None
        cleanup_ok = False
        try:
            result = task['_executor'].run(DEFAULT_TIMEOUT)
            cleanup_ok = SbxManager.get_instance().destroy_sandbox(
                sandbox_name
            )
            if not cleanup_ok:
                result = {
                    **(result or {}),
                    'returncode': -1,
                    'error': 'sandbox_cleanup_failed',
                }
            if task.get('_canceled'):
                result = {
                    **(result or {}),
                    'returncode': -1,
                    'timed_out': False,
                    'error': '用户手动取消',
                }

            finished_at = time.time()
            status = (
                'completed'
                if result.get('returncode') == 0
                and not result.get('timed_out')
                else 'failed'
            )
            self._db.update_task_result(
                task_id=task['task_id'],
                status=status,
                returncode=result.get('returncode', -1),
                stdout='',
                stderr='',
                timed_out=result.get('timed_out', False),
                error=result.get('error'),
                finished_at=finished_at,
            )
            self._db.cleanup_old_tasks(keep=MAX_COMPLETED_TASKS)
            with self._lock:
                task['result'] = result
                task['finished_at'] = finished_at
                task['status'] = status
                self._running.pop(task['task_id'], None)
                self._cv.notify()
            logger.info(
                '执行完成: %s status=%s returncode=%s',
                task['task_id'],
                status,
                result.get('returncode'),
            )
        except Exception as exc:
            logger.exception('任务 %s 异常', task['task_id'])
            self._db.update_task_result(
                task['task_id'],
                'failed',
                -1,
                '',
                '',
                error=(
                    '用户手动取消'
                    if task.get('_canceled')
                    else f'执行器异常: {exc}'
                ),
            )
        finally:
            if not cleanup_ok:
                try:
                    SbxManager.get_instance().destroy_sandbox(
                        sandbox_name
                    )
                except Exception:
                    logger.exception(
                        '任务 %s 清理 sandbox 失败',
                        task['task_id'],
                    )
            with self._lock:
                self._running.pop(task['task_id'], None)
                self._cv.notify()

    def _consume_loop(self):
        sbx = SbxManager.get_instance()
        while self._running_flag:
            task = None
            sandbox_name = ''
            try:
                with self._lock:
                    if not self._pending:
                        self._cv.wait(timeout=5)
                        continue
                    task_id = self._head_task_id()
                    candidate = dict(self._pending[task_id])

                allocated = sbx.allocate_sandbox(
                    owner=candidate['user_id'],
                    sandbox_id=candidate['task_id'],
                    cpu=candidate.get('cpu', 0),
                    mem=candidate.get('mem', '0'),
                    device_num=candidate.get('device_num', 0),
                    device_ids=candidate.get('device_ids') or None,
                )
                if allocated is None:
                    with self._lock:
                        self._cv.wait(timeout=3)
                    continue
                sandbox_name = allocated['sandbox_name']

                stale = False
                with self._lock:
                    if self._head_task_id() != task_id:
                        stale = True
                    else:
                        task = self._pending.pop(task_id)
                        task['status'] = 'running'
                        task['started_at'] = time.time()
                        task['devices'] = allocated['devices']
                        task['_executor'] = self._build_executor(
                            task,
                            sandbox_name,
                            task['devices'],
                        )
                        self._running[task_id] = task
                        self._reindex()
                if stale:
                    sbx.destroy_sandbox(sandbox_name)
                    continue

                self._db.update_task_status(
                    task_id,
                    'running',
                    started_at=task['started_at'],
                    devices=task['devices'],
                )
                thread = threading.Thread(
                    target=self._execute_one,
                    args=(task,),
                    daemon=True,
                    name=f'cmd-{task_id[:8]}',
                )
                thread.start()
            except Exception as exc:
                logger.exception('任务消费失败')
                if sandbox_name:
                    try:
                        sbx.destroy_sandbox(sandbox_name)
                    except Exception:
                        logger.exception('消费失败后的 sandbox 回收失败')
                if task:
                    self._db.update_task_result(
                        task['task_id'],
                        'failed',
                        -1,
                        '',
                        '',
                        error=f'sandbox 或执行器启动失败: {exc}',
                    )
                    with self._lock:
                        self._running.pop(task['task_id'], None)
                        self._cv.notify()
                time.sleep(1)

@command_bp.route('', methods=['POST'])
def create_task():
    """提交 Host 或现有 Docker 容器命令。"""
    body = request.get_json(silent=True) or {}
    command = (body.get('command') or '').strip()
    if not command:
        return {'error': '命令不能为空'}, 400

    user_id = (body.get('user_id') or '').strip()
    if not user_id:
        return {'error': 'user_id 不能为空'}, 400
    try:
        pwd.getpwnam(user_id)
    except KeyError:
        return {'error': f'系统用户 {user_id} 不存在'}, 400

    cpu = body.get('cpu', 0)
    if not isinstance(cpu, int) or cpu < 0:
        return {'error': 'cpu 必须是非负整数'}, 400

    memory = body.get('memory', 0)
    mem_unit = str(body.get('mem_unit', 'GB')).upper()
    if not isinstance(memory, int) or memory < 0:
        return {'error': 'memory 必须是非负整数'}, 400
    if mem_unit not in {'GB', 'MB'}:
        return {'error': 'mem_unit 必须是 GB 或 MB'}, 400
    sandbox_mem = (
        '0'
        if memory == 0
        else f'{memory}{"G" if mem_unit == "GB" else "M"}'
    )

    device_num = body.get('device_num', 0)
    if not isinstance(device_num, int) or device_num < 0:
        return {'error': 'device_num 必须是非负整数'}, 400

    sbx = SbxManager.get_instance()
    all_devices = sbx._discover_device_nodes()
    if device_num > len(all_devices):
        return {
            'error': (
                f'设备不足: 需要 {device_num} 个, '
                f'系统共 {len(all_devices)} 个'
            ),
        }, 400

    device_ids = body.get('device_ids')
    normalized_ids = None
    if device_ids:
        if not isinstance(device_ids, list):
            return {'error': 'device_ids 必须是数组'}, 400
        normalized_ids = _normalize_device_ids(device_ids, all_devices)
        if normalized_ids is None:
            return {
                'error': f'device_ids 包含不存在的设备: {device_ids}',
            }, 400

    try:
        target = normalize_execution_target(body.get('target'))
    except TargetValidationError as exc:
        return {'error': str(exc)}, 400

    if target['type'] == TARGET_DOCKER_EXISTING:
        if not (
            normalized_ids
            or device_num > 0
        ):
            return {
                'error': (
                    'docker_existing 必须通过 device_ids 或 '
                    'device_num 申请至少一张设备'
                ),
            }, 400

    est_time = body.get('est_time', 0)
    if not isinstance(est_time, int) or est_time < 0:
        est_time = 0

    try:
        task_id = TaskQueue.get_instance().submit(
            user_id=user_id,
            command=command,
            cpu=cpu,
            mem=sandbox_mem,
            device_num=0 if normalized_ids else device_num,
            device_ids=normalized_ids,
            target=target,
            est_time=est_time,
            priority=body.get('priority', 0),
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        # 数据层校验失败（如非法 priority）由 Database 层向上抛出
        return {'error': str(exc)}, 400
    task = Database.get_instance().get_task(task_id)
    position = task['position'] if task else 0
    return {
        'task_id': task_id,
        'position': position,
        'priority': task['priority'] if task else 0,
        'target': public_execution_target(target),
        'message': f'任务已提交，队列位置 #{position}',
    }, 202


@command_bp.route('', methods=['DELETE'])
def delete_tasks():
    """批量删除任务；运行中任务转为异步取消并保留结果/日志。

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


@command_bp.route('', methods=['GET'])
def list_tasks():
    """查看当前任务队列（所有用户的待执行 + 正在执行的任务，不含日志）。

    响应: { "queue": [...], "running": {...} }
    """
    tq = TaskQueue.get_instance()
    return {
        'queue': tq.get_queue(),
        'total_pending': len(tq._pending),
    }, 200


@command_bp.route('/<task_id>', methods=['GET'])
def get_task(task_id: str):
    """查看任务结果元数据（状态、返回码等，不含日志内容）。"""
    tq = TaskQueue.get_instance()
    result = tq.get_result(task_id)
    if result is None:
        return {'error': '任务不存在'}, 404
    return result, 200


@command_bp.route('/<task_id>/log', methods=['GET'])
def get_task_log(task_id: str):
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
