"""Host 命令执行器。

任务排队和 HTTP 路由分别位于 ``task_queue``、``task_api``；本模块只负责
在已创建的 sandbox 中启动一个 Host 进程并异步收集输出。
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
import signal
import time

from neu_box.runtime.sandbox import SbxManager
from neu_box.execution import logs
from neu_box.execution.process import CommandBackend, result

logger = logging.getLogger(__name__)


def command_timeout() -> int | None:
    from neu_box.config import env_int

    value = env_int("NEU_BOX_COMMAND_TIMEOUT", 0)
    return value if value > 0 else None


DEFAULT_TIMEOUT = command_timeout()


def _cgroup_procs_path(sandbox_name: str) -> str:
    return f"/sys/fs/cgroup/sandbox_{sandbox_name}/cgroup.procs"


def _process_state(pid: int) -> str:
    """Return the Linux process state (``T`` means stopped)."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("State:"):
                    return line.split()[1]
    except FileNotFoundError:
        return ""
    return ""




async def _wait_process(proc, timeout: float | None = None) -> bool:
    """Poll the asyncio child transport instead of waiting on SIGCHLD.

    Some supported Python/event-loop combinations can leave ``Process.wait``
    pending even after ``returncode`` has been populated.  Polling keeps task
    completion deterministic while still yielding to output consumption.
    Returns ``True`` when the timeout elapsed.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while proc.returncode is None:
        if deadline is not None and time.monotonic() >= deadline:
            return True
        await asyncio.sleep(0.01)
    return False


async def _execute_in_sandbox(
    command: str,
    sandbox_name: str,
    timeout: int | None = None,
    username: str = '',
) -> dict:
    """异步执行 Host 命令；输出写入任务日志，不创建读流线程。"""
    timeout = command_timeout() if timeout is None else timeout
    target_uid = target_gid = None
    target_dir = None
    if username:
        try:
            account = pwd.getpwnam(username)
        except KeyError:
            return result(stderr=f'Unknown user: {username}', error='unknown_user')
        target_uid, target_gid, target_dir = account.pw_uid, account.pw_gid, account.pw_dir

    proc = None
    reader = None
    output: list[str] = []
    log = logs.TaskLog(logs.log_path(logs.task_id_of(sandbox_name)))

    async def consume_output():
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    return
                text = chunk.decode('utf-8', errors='replace')
                output.append(text)
                log.write(text)
        except Exception as exc:
            logger.warning('读取 Host stdout 流异常: %s', exc)

    try:
        environment = {**os.environ, 'PYTHONUNBUFFERED': '1'}
        if username:
            environment['HOME'] = target_dir
        # The gate shell must not source a caller-controlled BASH_ENV before
        # it has joined the sandbox.  The actual command runs in the inner
        # bash after the parent explicitly opens the gate.
        environment.pop('BASH_ENV', None)
        environment.pop('ENV', None)
        logger.warning(
            '启动进程, cgroup=%s, user=%s',
            _cgroup_procs_path(sandbox_name), username or '(root)',
        )
        spawn_kwargs = {
            # The stop gate below makes stdin setup unnecessary.  DEVNULL
            # preserves the historical non-interactive command semantics.
            'stdin': asyncio.subprocess.DEVNULL,
            'stdout': asyncio.subprocess.PIPE,
            'stderr': asyncio.subprocess.STDOUT,
            'env': environment,
            'start_new_session': True,
        }
        if username:
            spawn_kwargs.update({
                'user': target_uid,
                'group': target_gid,
                'extra_groups': os.getgrouplist(username, target_gid),
                'cwd': target_dir,
            })
        proc = await asyncio.create_subprocess_exec(
            # Stop before evaluating the command.  The parent can then join
            # the exact PID into the sandbox without a create→join race,
            # while avoiding Python's unsafe preexec_fn in a multithreaded
            # worker.  This also works for malformed shell commands.
            '/bin/sh', '-c',
            # -i：让 bash source 完整 ~/.bashrc（绕过文件开头那段
            # *非交互* guard），用户的 conda/PATH/环境变量才在。
            'kill -STOP "$$"; exec /bin/bash -i -c "$1"',
            'neu-box', command,
            **spawn_kwargs,
        )
        deadline = time.monotonic() + 5
        while _process_state(proc.pid) not in {'T', 't'}:
            if proc.returncode is not None:
                return result(stderr='sandbox_gate_failed',
                               error='sandbox_gate_failed')
            if time.monotonic() >= deadline:
                raise RuntimeError('sandbox gate timeout')
            await asyncio.sleep(0.01)
        try:
            joined = SbxManager.get_instance().join_sandbox(sandbox_name, proc.pid)
        except Exception as exc:
            logger.error('join_sandbox 异常，终止未受约束进程: %s', exc)
            joined = False
        if not joined:
            logger.error('join_sandbox 返回失败，终止未受约束进程: %s', sandbox_name)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await _wait_process(proc, 5)
            return result(stderr='sandbox_join_failed', error='sandbox_join_failed')

        # Continuing the stopped process is the linearization point for
        # command execution.
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except ProcessLookupError:
            await _wait_process(proc, 5)
            return result(stderr='sandbox_join_failed', error='sandbox_join_failed')

        reader = asyncio.create_task(consume_output())
        timed_out = False
        if await _wait_process(proc, timeout):
            timed_out = True
            logger.warning('PID=%s 超时，正在终止...', proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                if await _wait_process(proc, 5):
                    raise TimeoutError
            except (ProcessLookupError, TimeoutError):
                proc.kill()
                await _wait_process(proc, 5)
        try:
            await asyncio.wait_for(reader, timeout=5)
        except asyncio.TimeoutError:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            logger.warning('任务日志输出流未在进程退出后结束: %s', sandbox_name)
        return result(-1 if timed_out else proc.returncode,
                       ''.join(output), timed_out=timed_out,
                       error='timeout' if timed_out else None)
    except Exception as exc:
        logger.exception('Host 命令执行异常')
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                await _wait_process(proc, 5)
            except (ProcessLookupError, OSError):
                pass
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        return result(stderr=f'Execution error: {exc}', error='exception')


class HostCommandExecutor(CommandBackend):
    """Host 目标的执行器。"""

    def __init__(self, *, task: dict, sandbox_name: str):
        self.task = task
        self.sandbox_name = sandbox_name

    async def run(self, timeout: int | None) -> dict:
        return await _execute_in_sandbox(
            self.task['command'], self.sandbox_name, timeout,
            self.task['user_id'],
        )

    def cancel(self):
        SbxManager.get_instance().destroy_sandbox(self.sandbox_name)


def execute_in_sandbox(command: str, sandbox_name: str,
                       timeout: int | None = None,
                       username: str = '') -> dict:
    """同步调用入口（测试、外部脚本用）。"""
    return asyncio.run(_execute_in_sandbox(command, sandbox_name, timeout, username))


def __getattr__(name: str):
    """Lazy compatibility export for the pre-refactor command blueprint.

    Older integrations imported ``command_bp`` from ``execution.host``.  A
    regular import here would cycle through ``TaskQueue → host`` while the
    new API module is being initialized, so resolve it only when an external
    caller asks for the attribute.
    """
    if name == 'command_bp':
        from neu_box.api import tasks
        return tasks.command_bp
    raise AttributeError(name)
