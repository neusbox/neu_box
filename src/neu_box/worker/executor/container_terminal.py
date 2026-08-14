"""把已运行 Docker 容器中的交互终端迁入或迁出 Neu Box sandbox。"""

from __future__ import annotations

import logging
import os
import signal
import time

from neu_box.worker.executor.command_docker import (
    ContainerProcess,
    DockerExecutorError,
    _expected_cgroup,
    _process_state,
    _read_unified_cgroup,
    verify_container_process,
)
from neu_box.worker.executor.sbx_manager import SbxManager


logger = logging.getLogger(__name__)


def _wait_stopped(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_state(pid) in {'T', 't'}:
            return
        time.sleep(0.02)
    raise DockerExecutorError(
        f'等待 PID {pid} 暂停超时',
        'docker_container_pid_stop_timeout',
    )


def _continue_process(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:
        pass


def _is_same_or_child_cgroup(path: str, parent: str) -> bool:
    path = path.rstrip('/') or '/'
    parent = parent.rstrip('/') or '/'
    if parent == '/':
        return True
    return path == parent or path.startswith(f'{parent}/')


def join_container_terminal(
    sbx: SbxManager,
    sandbox_name: str,
    process: ContainerProcess,
) -> None:
    """暂停、复核并迁移容器 shell；失败时尽力迁回 Docker cgroup。"""
    pid = process.host_pid
    expected = _expected_cgroup(sandbox_name)
    stopped = False
    try:
        os.kill(pid, signal.SIGSTOP)
        stopped = True
        _wait_stopped(pid)
        verify_container_process(process)

        current = _read_unified_cgroup(pid)
        if not _is_same_or_child_cgroup(current, process.container_cgroup):
            raise DockerExecutorError(
                f'容器 shell PID {pid} 已不在原 Docker cgroup: {current}',
                'docker_container_pid_cgroup_mismatch',
            )

        if not sbx.join_sandbox(sandbox_name, pid):
            raise DockerExecutorError(
                f'无法把容器 shell PID {pid} 加入沙盒',
                'docker_container_pid_join_failed',
            )
        actual = _read_unified_cgroup(pid)
        if actual != expected:
            raise DockerExecutorError(
                f'容器 shell cgroup 核验失败: '
                f'expected={expected} actual={actual}',
                'docker_container_pid_cgroup_verify_failed',
            )
        verify_container_process(process)
    except Exception as exc:
        try:
            if _read_unified_cgroup(pid) == expected:
                sbx.move_pid_to_cgroup(pid, process.container_cgroup)
        except (DockerExecutorError, OSError):
            logger.exception('容器 shell 迁移失败后的 cgroup 回滚检查失败')
        if isinstance(exc, DockerExecutorError):
            raise
        raise DockerExecutorError(
            f'迁移容器 shell 失败: {exc}',
            'docker_container_pid_join_failed',
        ) from exc
    finally:
        if stopped:
            _continue_process(pid)


def restore_container_terminal(
    sbx: SbxManager,
    sandbox_name: str,
    shell: ContainerProcess,
    client: ContainerProcess,
) -> None:
    """把 shell 和本次 release HTTP 客户端迁回原 Docker cgroup。"""
    if (
        shell.container_id != client.container_id
        or shell.init_host_pid != client.init_host_pid
        or shell.container_started_at != client.container_started_at
        or shell.container_cgroup != client.container_cgroup
    ):
        raise DockerExecutorError(
            'shell 与 release 客户端不属于同一个容器实例',
            'docker_container_release_identity_mismatch',
        )

    expected_sandbox = _expected_cgroup(sandbox_name)
    target = shell.container_cgroup
    for role, process in (('shell', shell), ('client', client)):
        current = _read_unified_cgroup(process.host_pid)
        if current not in {expected_sandbox, target}:
            raise DockerExecutorError(
                f'{role} PID {process.host_pid} 不属于待释放沙盒或目标容器: '
                f'{current}',
                'docker_container_release_cgroup_mismatch',
            )

    stopped = False
    try:
        os.kill(shell.host_pid, signal.SIGSTOP)
        stopped = True
        _wait_stopped(shell.host_pid)
        verify_container_process(shell)
        verify_container_process(client)

        for process in (shell, client):
            if _read_unified_cgroup(process.host_pid) == target:
                continue
            if not sbx.move_pid_to_cgroup(process.host_pid, target):
                raise DockerExecutorError(
                    f'无法将 PID {process.host_pid} 迁回 Docker cgroup',
                    'docker_container_release_restore_failed',
                )
    except DockerExecutorError:
        raise
    except Exception as exc:
        raise DockerExecutorError(
            f'恢复容器终端 cgroup 失败: {exc}',
            'docker_container_release_restore_failed',
        ) from exc
    finally:
        if stopped:
            _continue_process(shell.host_pid)
