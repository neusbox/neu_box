"""在现有 Docker 容器中执行受 sandbox cgroup 约束的命令。

Docker Exec 创建的进程先自 SIGSTOP。Worker 核对它的容器身份后把 host
PID 加入任务 sandbox，再发送 SIGCONT；容器生命周期仍由 Docker 管理。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from neu_box.worker.executor.sbx_manager import SbxManager


logger = logging.getLogger(__name__)

_DOCKER_API_TIMEOUT = 3600
_EXEC_STOP_TIMEOUT = 15.0
_POLL_INTERVAL = 0.05


class DockerExecutorError(RuntimeError):
    """Docker 目标无法安全启动或执行。"""

    def __init__(self, message: str, code: str = 'docker_execution_failed'):
        self.code = code
        super().__init__(message)


def _load_docker():
    try:
        import docker
    except ImportError as exc:
        raise DockerExecutorError(
            'Worker 未包含 Docker SDK；开发环境请执行 uv sync --extra worker',
            'docker_sdk_unavailable',
        ) from exc
    return docker


def _docker_client():
    docker = _load_docker()
    try:
        client = docker.from_env(timeout=_DOCKER_API_TIMEOUT)
        client.ping()
        return client
    except Exception as exc:
        raise DockerExecutorError(
            f'无法连接 Docker Engine: {exc}',
            'docker_unavailable',
        ) from exc


def _expected_cgroup(sandbox_name: str) -> str:
    return f'/sandbox_{sandbox_name}'


def _read_unified_cgroup(pid: int) -> str:
    with open(f'/proc/{pid}/cgroup', encoding='utf-8') as stream:
        for line in stream:
            hierarchy, _controllers, path = line.rstrip('\n').split(':', 2)
            if hierarchy == '0':
                return path
    raise DockerExecutorError(
        f'PID {pid} 没有 cgroup v2 记录',
        'docker_exec_pid_invalid',
    )


def _process_state(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/status', encoding='utf-8') as stream:
            for line in stream:
                if line.startswith('State:'):
                    return line.split()[1]
    except FileNotFoundError:
        return ''
    return ''


def _namespace_inode(pid: int, namespace: str) -> int:
    return os.stat(f'/proc/{pid}/ns/{namespace}').st_ino


def _same_container_namespaces(exec_pid: int, init_pid: int) -> bool:
    for namespace in ('mnt', 'pid', 'net', 'ipc', 'uts', 'user', 'cgroup'):
        try:
            if _namespace_inode(exec_pid, namespace) != _namespace_inode(
                init_pid, namespace,
            ):
                return False
        except FileNotFoundError:
            return False
    return True

def _is_container_cgroup(pid: int, init_pid: int) -> bool:
    """PID 是否仍位于目标容器 cgroup（允许其任意后代 cgroup）。"""
    init_cgroup = _read_unified_cgroup(init_pid).rstrip('/')
    pid_cgroup = _read_unified_cgroup(pid).rstrip('/')
    return (
        pid_cgroup == init_cgroup
        or pid_cgroup.startswith(f'{init_cgroup}/')
    )


class _TaskLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, chunk: bytes | str | None):
        if not chunk:
            return
        if isinstance(chunk, bytes):
            text = chunk.decode('utf-8', errors='replace')
        else:
            text = str(chunk)
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()


def _result(
    returncode: int = -1,
    stdout: str = '',
    stderr: str = '',
    timed_out: bool = False,
    error: str | None = None,
) -> dict:
    return {
        'returncode': returncode,
        'stdout': stdout,
        'stderr': stderr,
        'timed_out': timed_out,
        'error': error,
    }


class ExistingDockerCommandExecutor:
    """在现有容器中执行一次受 task cgroup 约束的命令。"""

    def __init__(
        self,
        *,
        task: dict,
        sandbox_name: str,
        devices: list[str],
        log_path: str,
    ):
        self.task = task
        self.sandbox_name = sandbox_name
        self.devices = list(devices)
        self.target = dict(
            task.get('target_spec')
            or task.get('target')
            or {'type': 'host'}
        )
        self.log = _TaskLog(log_path)
        self.sbx = SbxManager.get_instance()
        self._cancel_event = threading.Event()
        self._runtime_lock = threading.Lock()
        self._runtime_pid = 0
        self._client = None
        self._exec_id = ''
        self._container_id = ''
        self._container_init_pid = 0

    def _set_runtime_pid(self, pid: int):
        with self._runtime_lock:
            self._runtime_pid = int(pid or 0)

    def cancel(self):
        """只按当前 ExecInspect 身份终止本次 exec，避免旧 PID 复用误杀。"""
        self._cancel_event.set()
        client = self._client
        exec_id = self._exec_id
        if client is None or not exec_id:
            return
        try:
            info = client.api.exec_inspect(exec_id)
            if not info.get('Running'):
                return
            if (
                str(info.get('ContainerID') or '')
                not in {'', self._container_id}
            ):
                logger.error('取消 Docker exec 时 ContainerID 已变化')
                return
            pid = int(info.get('Pid') or 0)
            with self._runtime_lock:
                expected_pid = self._runtime_pid
            if pid <= 0 or (expected_pid and pid != expected_pid):
                logger.error(
                    '取消 Docker exec 时 PID 身份不匹配: api=%s expected=%s',
                    pid, expected_pid,
                )
                return
            if (
                self._container_init_pid <= 0
                or not _same_container_namespaces(
                    pid, self._container_init_pid,
                )
            ):
                logger.error('取消 Docker exec 时 namespace 身份不匹配')
                return
            pid_cgroup = _read_unified_cgroup(pid)
            if (
                pid_cgroup != _expected_cgroup(self.sandbox_name)
                and not _is_container_cgroup(
                    pid, self._container_init_pid,
                )
            ):
                logger.error('取消 Docker exec 时 cgroup 身份不匹配')
                return
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception('终止 Docker exec 失败')

    @staticmethod
    def _wrapper(command: str) -> list[str]:
        # $1 作为一个 argv 元素传入，避免把用户命令拼入外层 gate shell。
        return [
            '/bin/sh',
            '-c',
            'kill -STOP "$$"; exec /bin/sh -c "$1"',
            'neu-box',
            command,
        ]

    def _validate_container(self, container):
        container.reload()
        state = container.attrs.get('State') or {}
        if not state.get('Running') or state.get('Paused'):
            raise DockerExecutorError(
                '目标容器必须处于 running 且未 paused',
                'docker_container_not_running',
            )
        init_pid = int(state.get('Pid') or 0)
        if init_pid <= 0 or not os.path.exists(f'/proc/{init_pid}'):
            raise DockerExecutorError(
                '无法取得目标容器 init host PID',
                'docker_container_pid_invalid',
            )
        visible = set(self.sbx._discover_device_nodes(
            f'/proc/{init_pid}/root/dev'
        ))
        missing = sorted(set(self.devices) - visible)
        if missing:
            raise DockerExecutorError(
                f'目标容器没有挂载沙盒设备节点: {missing}',
                'docker_devices_not_visible',
            )
        return init_pid, str(state.get('StartedAt') or '')

    def _assert_container_unchanged(
        self,
        container,
        expected_init_pid: int,
        expected_started_at: str,
    ):
        container.reload()
        state = container.attrs.get('State') or {}
        if (
            container.id != self._container_id
            or not state.get('Running')
            or int(state.get('Pid') or 0) != expected_init_pid
            or str(state.get('StartedAt') or '') != expected_started_at
        ):
            raise DockerExecutorError(
                '目标容器在 exec 启动期间停止、重启或被替换',
                'docker_container_changed',
            )

    def _assert_exec_unchanged(self, pid: int):
        info = self._client.api.exec_inspect(self._exec_id)
        if (
            not info.get('Running')
            or int(info.get('Pid') or 0) != pid
            or str(info.get('ContainerID') or '') not in {
                '', self._container_id,
            }
        ):
            raise DockerExecutorError(
                f'Docker exec 身份在迁移前发生变化: {info}',
                'docker_exec_identity_changed',
            )

    def _wait_stopped_pid(self, exec_id: str) -> int:
        deadline = time.monotonic() + _EXEC_STOP_TIMEOUT
        last = {}
        while time.monotonic() < deadline:
            last = self._client.api.exec_inspect(exec_id)
            pid = int(last.get('Pid') or 0)
            if pid:
                self._set_runtime_pid(pid)
            if pid and _process_state(pid) in {'T', 't'}:
                return pid
            if (
                not last.get('Running')
                and last.get('ExitCode') is not None
            ):
                raise DockerExecutorError(
                    f'Docker exec 在自停前退出: {last}',
                    'docker_exec_stopped_early',
                )
            if self._cancel_event.is_set():
                if pid:
                    self._set_runtime_pid(pid)
                    self.cancel()
                raise DockerExecutorError('任务已取消', 'canceled')
            time.sleep(_POLL_INTERVAL)
        self.cancel()
        raise DockerExecutorError(
            f'等待 Docker exec 自 SIGSTOP 超时: {last}',
            'docker_exec_stop_timeout',
        )

    def _verify_exec_pid(self, pid: int, init_pid: int):
        if _process_state(pid) not in {'T', 't'}:
            raise DockerExecutorError(
                f'Docker exec PID {pid} 未处于 stopped 状态',
                'docker_exec_not_stopped',
            )
        if not _same_container_namespaces(pid, init_pid):
            raise DockerExecutorError(
                f'Docker exec PID {pid} 不属于目标容器 namespace',
                'docker_exec_namespace_mismatch',
            )
        init_cgroup = _read_unified_cgroup(init_pid).rstrip('/')
        exec_cgroup = _read_unified_cgroup(pid).rstrip('/')
        if (
            exec_cgroup != init_cgroup
            and not exec_cgroup.startswith(f'{init_cgroup}/')
        ):
            raise DockerExecutorError(
                f'Docker exec PID cgroup 不属于目标容器: '
                f'init={init_cgroup} exec={exec_cgroup}',
                'docker_exec_cgroup_mismatch',
            )

    def _join_and_continue(self, pid: int):
        if not self.sbx.join_sandbox(self.sandbox_name, pid):
            raise DockerExecutorError(
                f'无法把 Docker exec PID {pid} 加入任务 cgroup',
                'docker_exec_cgroup_join_failed',
            )
        actual = _read_unified_cgroup(pid)
        expected = _expected_cgroup(self.sandbox_name)
        if actual != expected:
            raise DockerExecutorError(
                f'Docker exec PID cgroup 核验失败: '
                f'expected={expected} actual={actual}',
                'docker_exec_cgroup_verify_failed',
            )
        self._assert_exec_unchanged(pid)
        if self._cancel_event.is_set():
            self.cancel()
            raise DockerExecutorError('任务已取消', 'canceled')
        os.kill(pid, signal.SIGCONT)

    def run(self, timeout: int | None) -> dict:
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stream_errors: list[str] = []
        stream_thread = None
        timed_out = False
        pid = 0
        try:
            self._client = _docker_client()
            docker = _load_docker()
            try:
                container = self._client.containers.get(
                    self.target['container']
                )
            except docker.errors.NotFound as exc:
                raise DockerExecutorError(
                    f'目标容器不存在: {self.target["container"]}',
                    'docker_container_not_found',
                ) from exc

            self._container_id = container.id
            init_pid, started_at = self._validate_container(container)
            self._container_init_pid = init_pid

            exec_kwargs = {
                'container': container.id,
                'cmd': self._wrapper(self.task['command']),
                'stdout': True,
                'stderr': True,
                'stdin': False,
                'tty': False,
                'privileged': False,
            }
            if self.target.get('workdir'):
                exec_kwargs['workdir'] = self.target['workdir']
            if self.target.get('env'):
                exec_kwargs['environment'] = [
                    f'{key}={value}'
                    for key, value in self.target['env'].items()
                ]
            if self.target.get('user'):
                exec_kwargs['user'] = self.target['user']

            created = self._client.api.exec_create(**exec_kwargs)
            self._exec_id = created['Id']

            def consume_output():
                try:
                    stream = self._client.api.exec_start(
                        self._exec_id,
                        detach=False,
                        tty=False,
                        stream=True,
                        demux=True,
                    )
                    for out, err in stream:
                        if out:
                            text = out.decode('utf-8', errors='replace')
                            stdout_chunks.append(text)
                            self.log.write(text)
                        if err:
                            text = err.decode('utf-8', errors='replace')
                            stderr_chunks.append(text)
                            self.log.write(text)
                except Exception as exc:
                    stream_errors.append(str(exc))

            stream_thread = threading.Thread(
                target=consume_output,
                daemon=True,
                name=f'docker-exec-{self.task["task_id"][:8]}',
            )
            stream_thread.start()

            pid = self._wait_stopped_pid(self._exec_id)
            self._assert_container_unchanged(
                container, init_pid, started_at,
            )
            self._assert_exec_unchanged(pid)
            self._verify_exec_pid(pid, init_pid)
            self._join_and_continue(pid)

            deadline = time.monotonic() + timeout if timeout else None
            while True:
                info = self._client.api.exec_inspect(self._exec_id)
                if not info.get('Running'):
                    returncode = int(info.get('ExitCode', -1))
                    break
                if self._cancel_event.is_set():
                    self.cancel()
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    self.cancel()
                time.sleep(0.1)

            if stream_thread is not None:
                stream_thread.join(timeout=10)
            if stream_thread is not None and stream_thread.is_alive():
                stream_errors.append('Docker exec 输出流未在退出后结束')
            if stream_errors:
                raise DockerExecutorError(
                    f'读取 Docker exec 输出失败: {stream_errors}',
                    'docker_exec_stream_failed',
                )
            return _result(
                returncode=-1 if timed_out else returncode,
                stdout=''.join(stdout_chunks),
                stderr=''.join(stderr_chunks),
                timed_out=timed_out,
                error='timeout' if timed_out else None,
            )
        except DockerExecutorError as exc:
            if pid:
                self.cancel()
            self.log.write(f'\n[neu_box] {exc}\n')
            return _result(stderr=str(exc), error=exc.code)
        except Exception as exc:
            logger.exception('现有 Docker 容器命令执行失败')
            self.cancel()
            self.log.write(f'\n[neu_box] Docker exec error: {exc}\n')
            return _result(
                stderr=f'Docker exec error: {exc}',
                error='docker_exec_exception',
            )
        finally:
            if stream_thread is not None:
                stream_thread.join(timeout=2)
            if self._client is not None:
                self._client.close()
