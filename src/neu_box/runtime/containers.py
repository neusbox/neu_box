"""容器身份：把"一个容器"解析成宿主机侧可判定的身份。

受管容器一律是 neu-box 起的一次性容器，所以这里不做"在已有容器里 exec"的
PID 映射，只回答三个问题:

  1. 这个容器现在跑着吗，init host PID 是多少
  2. 它的 mount namespace inum 是多少（BPF 判定用的身份 key）
  3. 这个身份还是刚才那个吗（防 PID 复用）

身份 key 用 mount namespace: docker 一定给容器一个新的 mnt ns，没有参数能
关掉（``--pid=host`` / ``--net=host`` 关的是别的 namespace）；而且驱动建 UDA
设备表用的就是同一个 key，我们的边界和驱动的边界同宽。两侧取值天然一致 ——
``stat("/proc/<pid>/ns/mnt").st_ino`` 就是 BPF 里 CO-RE 读到的 ``ns.inum``，
不需要任何换算。

授权本身不在这里判断: 这个模块只产出身份，谁能拿到设备由 BPF 按
``container_owner`` 归属表决定。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Identity lookups are also exposed through the HTTP registration endpoint;
# never let a stalled Docker daemon hold a request thread for an hour.
CONTAINER_IDENTITY_TIMEOUT = 30
_DOCKER_API_TIMEOUT = 3600


class DockerExecutorError(RuntimeError):
    """容器目标无法安全启动、解析或执行。"""

    def __init__(self, message: str, code: str = 'docker_execution_failed'):
        self.code = code
        super().__init__(message)


def load_docker():
    try:
        import docker
    except ImportError as exc:
        raise DockerExecutorError(
            'Worker 发布包缺少 Docker SDK，请重新构建并部署完整发布包',
            'docker_sdk_unavailable',
        ) from exc
    return docker


def docker_client(timeout: int = _DOCKER_API_TIMEOUT):
    docker = load_docker()
    try:
        client = docker.from_env(timeout=timeout)
        client.ping()
        return client
    except Exception as exc:
        raise DockerExecutorError(
            f'无法连接 Docker Engine: {exc}',
            'docker_unavailable',
        ) from exc


def read_unified_cgroup(pid: int) -> str:
    """读取 PID 在 cgroup v2 统一层级里的路径。"""
    with open(f'/proc/{pid}/cgroup', encoding='utf-8') as stream:
        for line in stream:
            hierarchy, _controllers, path = line.rstrip('\n').split(':', 2)
            if hierarchy == '0':
                return path
    raise DockerExecutorError(
        f'PID {pid} 没有 cgroup v2 记录',
        'docker_container_pid_invalid',
    )


def namespace_inode(pid: int, namespace: str) -> int:
    return os.stat(f'/proc/{pid}/ns/{namespace}').st_ino


def mount_namespace_of(pid: int) -> int:
    """容器身份 key。和 BPF 读到的 ``ns.inum`` 是同一个数。"""
    try:
        return namespace_inode(pid, 'mnt')
    except FileNotFoundError as exc:
        raise DockerExecutorError(
            f'PID {pid} 已不存在，无法读取 mount namespace',
            'docker_container_pid_invalid',
        ) from exc


def process_start_time(pid: int) -> int:
    """读取 /proc/<pid>/stat 的 starttime，用于防止 PID 复用。"""
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as stream:
            raw = stream.read().strip()
        # comm 字段允许包含空格和括号；最后一个 ')' 之后从字段 3 开始。
        fields = raw.rsplit(')', 1)[1].split()
        return int(fields[19])
    except (FileNotFoundError, IndexError, ValueError, OSError) as exc:
        raise DockerExecutorError(
            f'无法读取 PID {pid} 的启动时间',
            'docker_container_pid_invalid',
        ) from exc


def process_state(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/status', encoding='utf-8') as stream:
            for line in stream:
                if line.startswith('State:'):
                    return line.split()[1]
    except FileNotFoundError:
        return ''
    return ''


def same_container_namespaces(left_pid: int, right_pid: int) -> bool:
    for namespace in ('mnt', 'pid', 'net', 'ipc', 'uts', 'user', 'cgroup'):
        try:
            if namespace_inode(left_pid, namespace) != namespace_inode(
                right_pid, namespace,
            ):
                return False
        except FileNotFoundError:
            return False
    return True


@dataclass(frozen=True)
class ContainerIdentity:
    """一个容器的稳定宿主机侧身份。"""

    container_ref: str
    container_id: str
    init_host_pid: int
    init_start_time: int
    mount_namespace: int
    container_cgroup: str
    started_at: str

    def is_alive(self) -> bool:
        """容器是否还是登记时的那个容器（同时覆盖 PID 复用）。"""
        return identity_alive(
            init_host_pid=self.init_host_pid,
            init_start_time=self.init_start_time,
            mount_namespace=self.mount_namespace,
        )


def identity_alive(
    *,
    init_host_pid: int,
    init_start_time: int,
    mount_namespace: int,
) -> bool:
    """按登记时记下的三个数判断容器是否还活着。

    只看 /proc 就够，不需要问 Docker: starttime 挡住 PID 复用，mnt ns 再确认
    一次是同一个容器。两者任一不符都当作容器已经结束。
    """
    try:
        if process_start_time(init_host_pid) != init_start_time:
            return False
        return mount_namespace_of(init_host_pid) == mount_namespace
    except DockerExecutorError:
        return False


def container_identity(
    container_ref: str, timeout: int = _DOCKER_API_TIMEOUT,
) -> ContainerIdentity:
    """解析容器身份。解析和校验只在这里做，产出止于 init host PID。"""
    container_ref = str(container_ref or '').strip()
    if not container_ref:
        raise DockerExecutorError(
            'container 不能为空',
            'docker_container_required',
        )

    client = docker_client(timeout=timeout)
    try:
        container = client.containers.get(container_ref)
        container.reload()
    except DockerExecutorError:
        raise
    except Exception as exc:
        raise DockerExecutorError(
            f'找不到容器 {container_ref}: {exc}',
            'docker_container_not_found',
        ) from exc
    finally:
        try:
            client.close()
        except Exception:
            logger.exception('关闭 Docker client 失败')

    state = container.attrs.get('State') or {}
    if not state.get('Running') or state.get('Paused'):
        raise DockerExecutorError(
            f'容器 {container_ref} 必须处于 running 且未 paused',
            'docker_container_not_running',
        )

    init_pid = int(state.get('Pid') or 0)
    if init_pid <= 0 or not os.path.exists(f'/proc/{init_pid}'):
        raise DockerExecutorError(
            f'无法取得容器 {container_ref} 的 init host PID',
            'docker_container_pid_invalid',
        )

    try:
        cgroup = read_unified_cgroup(init_pid)
    except DockerExecutorError:
        raise
    except Exception as exc:
        raise DockerExecutorError(
            f'无法读取容器 {container_ref} 的 cgroup: {exc}',
            'docker_container_pid_invalid',
        ) from exc

    mount_namespace = mount_namespace_of(init_pid)
    # 容器一定有自己的一份 mount namespace（docker 没有关掉它的开关）。
    # 和宿主机相同说明这个 PID 不是容器进程 —— 拿它去登记等于把整个宿主
    # 机变成受托方，必须在解析这一步就挡掉，而不是把判断散到各个调用点。
    if mount_namespace == namespace_inode(os.getpid(), 'mnt'):
        raise DockerExecutorError(
            f'{container_ref} 与宿主机共用 mount namespace，不是容器',
            'docker_container_same_mount_namespace',
        )

    return ContainerIdentity(
        container_ref=container_ref,
        container_id=str(container.id),
        init_host_pid=init_pid,
        init_start_time=process_start_time(init_pid),
        mount_namespace=mount_namespace,
        container_cgroup=cgroup,
        started_at=str(state.get('StartedAt') or ''),
    )


def runtime_container_identity(
    container_id: str, init_host_pid: int,
) -> ContainerIdentity:
    """Build identity from OCI runtime state without querying Docker.

    Runtime hooks run from inside Docker's create path.  Calling the Docker
    API from that path can recursively re-enter an authorization plugin, so
    this variant relies only on the trusted OCI PID and ``/proc``.
    """
    container_id = str(container_id or '').strip()
    if not container_id:
        raise DockerExecutorError('container_id 不能为空', 'docker_container_required')
    try:
        pid = int(init_host_pid)
    except (TypeError, ValueError) as exc:
        raise DockerExecutorError('host_pid 必须为正整数', 'docker_container_pid_invalid') from exc
    if pid <= 0 or not os.path.exists(f'/proc/{pid}'):
        raise DockerExecutorError(f'容器 PID {pid} 不存在', 'docker_container_pid_invalid')
    try:
        cgroup = read_unified_cgroup(pid)
        mount_namespace = mount_namespace_of(pid)
        start_time = process_start_time(pid)
    except DockerExecutorError:
        raise
    except Exception as exc:
        raise DockerExecutorError(
            f'读取 OCI 容器身份失败: {exc}', 'docker_container_pid_invalid',
        ) from exc
    if mount_namespace == namespace_inode(os.getpid(), 'mnt'):
        raise DockerExecutorError(
            f'{container_id} 与宿主机共用 mount namespace，不是容器',
            'docker_container_same_mount_namespace',
        )
    return ContainerIdentity(
        container_ref=container_id,
        container_id=container_id,
        init_host_pid=pid,
        init_start_time=start_time,
        mount_namespace=mount_namespace,
        container_cgroup=cgroup,
        started_at='',
    )


def verify_identity(identity: ContainerIdentity) -> None:
    """复核身份未被替换，避免登记前后容器被重建。"""
    if process_start_time(identity.init_host_pid) != identity.init_start_time:
        raise DockerExecutorError(
            f'容器 {identity.container_ref} 已重启或 init 进程已变化',
            'docker_container_changed',
        )
    if mount_namespace_of(identity.init_host_pid) != identity.mount_namespace:
        raise DockerExecutorError(
            f'容器 {identity.container_ref} 的 mount namespace 已变化',
            'docker_container_changed',
        )
