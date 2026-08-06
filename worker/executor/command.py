"""命令执行模块 — 在沙盒中安全执行用户提交的命令。

架构:
  TaskQueue (FIFO)  →  sandbox 设备分配  →  Host / Docker Exec

API:
  POST /command/run           提交命令（异步），返回 task_id
  GET  /command/queue         查看所有任务队列（公开元数据，不含日志）
  GET  /command/result/<id>   查看自己的任务结果（含日志，需 user_id 匹配）
"""

import logging
import os
import pwd
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import OrderedDict

from flask import Blueprint, request

from executor.sbx_manager import SbxManager
from executor.command_docker import (
    ExistingDockerCommandExecutor,
    recover_orphaned_docker_task,
)
from executor.command_target import (
    TARGET_DOCKER_EXISTING,
    TARGET_HOST,
    TargetValidationError,
    normalize_execution_target,
    public_execution_target,
    public_runtime_metadata,
)

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

    stderr 已在 shell 层 2>&1 合并到 stdout，保证输出按时间序排列。

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

from executor.db import Database



class TaskQueue:
    """每个 Worker 一个 FIFO 命令队列。"""

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
        """回收上次运行中的 sandbox/Docker Exec，并恢复排队任务。"""
        failures = []
        for task in self._db.get_queue_tasks():
            if task['status'] == 'running':
                sandbox_name = self._sandbox_name(task)
                recovery_task = dict(task)
                recovery_task['sandbox_name'] = sandbox_name
                errors = []
                try:
                    recover_orphaned_docker_task(recovery_task)
                except Exception as exc:
                    errors.append(f'Docker Exec: {exc}')
                    logger.exception('恢复 Docker Exec %s 失败', task['task_id'])
                try:
                    if not SbxManager.get_instance().destroy_sandbox(
                        sandbox_name
                    ):
                        errors.append('sandbox 清理失败')
                except Exception as exc:
                    errors.append(f'sandbox: {exc}')
                    logger.exception('恢复 sandbox %s 失败', sandbox_name)
                if errors:
                    failures.append(
                        f'{task["task_id"]}: {"；".join(errors)}'
                    )
                    continue
                self._db.update_task_result(
                    task['task_id'],
                    'failed',
                    -1,
                    '',
                    '',
                    error='Worker 在执行过程中重启',
                )
                continue

            sandbox_name = self._sandbox_name(task)
            if not SbxManager.get_instance().destroy_sandbox(sandbox_name):
                failures.append(
                    f'{task["task_id"]}: queued sandbox 清理失败'
                )
                continue
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
                'devices': [],
                'status': 'queued',
                'position': 0,
                'created_at': task.get('created_at', time.time()),
                'started_at': None,
                'finished_at': None,
                'result': None,
            }

        if self._pending:
            self._reindex()
            logger.info('恢复完成: %s 个任务重新入队', len(self._pending))
        if failures:
            raise RuntimeError(
                '运行中任务尚未安全回收，拒绝 Worker 上线: '
                + ' | '.join(failures)
            )

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
            '任务入队: %s user=%s cmd=%s...',
            task_id,
            user_id,
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

    def _reindex(self):
        task_ids = list(self._pending)
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
            'runtime': public_runtime_metadata(
                task.get('runtime_metadata')
            ),
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
                    task_id = next(iter(self._pending))
                    candidate = dict(self._pending[task_id])

                allocated = sbx.allocate_for_terminal(
                    owner=candidate['user_id'],
                    terminal_id=candidate['task_id'],
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
                    if (
                        task_id not in self._pending
                        or next(iter(self._pending), None) != task_id
                    ):
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

@command_bp.route('/run', methods=['POST'])
def run_command():
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
                    'device_num 申请至少一张 NPU'
                ),
            }, 400
        if not re.fullmatch(sbx.device_filter or r'(?!x)x', 'davinci0'):
            return {
                'error': 'docker_existing 仅支持 Ascend NPU Worker',
            }, 400

    est_time = body.get('est_time', 0)
    if not isinstance(est_time, int) or est_time < 0:
        est_time = 0

    task_id = TaskQueue.get_instance().submit(
        user_id=user_id,
        command=command,
        cpu=cpu,
        mem=sandbox_mem,
        device_num=0 if normalized_ids else device_num,
        device_ids=normalized_ids,
        target=target,
        est_time=est_time,
    )
    task = Database.get_instance().get_task(task_id)
    position = task['position'] if task else 0
    return {
        'task_id': task_id,
        'position': position,
        'target': public_execution_target(target),
        'message': f'任务已提交，队列位置 #{position}',
    }, 202


@command_bp.route('/tasks/delete', methods=['POST'])
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


# ── 启动 TaskQueue（在 import 时自动启动） ──
TaskQueue.get_instance().start()
