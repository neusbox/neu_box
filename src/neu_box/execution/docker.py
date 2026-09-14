"""一次性 Docker 容器任务的执行后端。

容器授权由节点级 OCI runtime 在容器 ENTRYPOINT 之前完成：Worker 只把沙盒名
写成 ``sandbox_cgroup`` annotation（``docs/container-registration.md``），由 runtime hook
去登记，然后等容器结束。容器内不再挂载 neu-sbox，也不再使用 STOP/CONT 或
HTTP gate；Worker 自己也不补登记。

登记为什么必须卡在这个窗口里 —— 这就是这套设计的存在理由:

驱动的 UDA 设备表在第一次 NPU 初始化时按当时的权限建出来，之后以 mnt ns 为
key 缓存、被同一容器里的所有进程复用。登记晚了就会建出一张空表并被一直复用，
之后即使补上登记也恢复不了（fail-closed，但表现成"驱动装了没生效"）。所以
``_run_blocking`` 只**检查** hook 已经登记（容器还看得见时；几毫秒就跑完的
短命令两个观测点都已消失，那时只剩对 runtime 的信任，见 ``_run_blocking``），
绝不做事后补登记。

设备节点全给容器（受管卡 + 驱动初始化要的管理设备，清单见
``runtime/devices.py`` 的 ``node_paths``）: 强制点是 BPF 对 davinciN 的 open
判定，不是节点在不在容器的 /dev 里；而驱动本身又必须拿到 manager / hdc 才能
初始化。限额落在 docker 的 flag 上（容器不在沙盒 cgroup 树里），所以 cpu/mem
是"每个容器一份"。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from neu_box.execution import logs
from neu_box.execution.process import CommandBackend, result
from neu_box.runtime import cgroup, devices
from neu_box.runtime.containers import (
    ContainerIdentity,
    DockerExecutorError,
    container_identity,
    docker_client,
    mount_namespace_of,
    process_start_time,
)
from neu_box.runtime.sandbox import SbxManager

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 0.05
_REGISTRATION_TIMEOUT = 30.0
_DOCKER_API_TIMEOUT = 30
def _memory_bytes(value) -> int:
    """把 "2G" / "512M" / "0" 换算成字节数。"""
    text = str(value or '0').strip()
    if not text or text == '0':
        return 0
    multiplier = 1
    suffix = text[-1].upper()
    if suffix in {'K', 'M', 'G', 'T'}:
        multiplier = 1024 ** ('KMGT'.index(suffix) + 1)
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


class DockerCommandExecutor(CommandBackend):
    """起一个一次性容器执行命令；容器生命周期仍归 docker 管。"""

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
        self.log = logs.TaskLog(log_path)
        self.sbx = SbxManager.get_instance()
        self._cancel_event = threading.Event()
        self._runtime_lock = threading.Lock()
        self._client = None
        self._container_id = ''
        self._init_host_pid = 0
        self._init_start_time = 0
        self._mount_namespace = 0
        self._log_thread = None

    # ── 取消 ─────────────────────────────────────────────────────

    def cancel(self):
        """按记下的 init 进程身份终止容器，避免旧 PID 复用误杀。"""
        self._cancel_event.set()
        client = self._client
        container_id = self._container_id
        init_pid, init_start = self._init_host_pid, self._init_start_time
        if client is None or not container_id:
            return
        try:
            if init_pid and process_start_time(init_pid) == init_start:
                os.kill(init_pid, signal.SIGKILL)
                return
            self._kill(client)
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception('终止容器 %s 失败', container_id)

    # ── 启动 ─────────────────────────────────────────────────────

    def _limits(self) -> dict:
        limits = {}
        cpu = self.task.get('cpu', 0) or 0
        if isinstance(cpu, int) and cpu > 0:
            limits['cpu_period'] = 100000
            limits['cpu_quota'] = cpu * 100000
        memory = _memory_bytes(self.task.get('mem', '0'))
        if memory > 0:
            limits['mem_limit'] = memory
            # 不显式给 memswap 的话 swap 会从"禁用"变成默认两倍。
            limits['memswap_limit'] = memory
        return limits

    def _start_container(self, client) -> str:
        """起容器。这里**绝不能持有** SbxManager 的任何锁。

        Worker 调 Docker 建容器会阻塞到容器起来，而 runtime hook 恰好在这个
        窗口里回调 Worker 自己的 /container/register；那个端点要拿
        ``lifecycle_lock`` / ``self.lock`` / ``_container_registration_lock``。
        在这里持锁就是自己等自己。
        """
        image_ref = str(self.target.get('image')).strip()
        command = str(self.task.get('command') or '')
        environment = dict(self.target.get('env') or {})

        host_config = client.api.create_host_config(
            devices=devices.node_paths() or None,
            **self._limits(),
        )
        # annotation 是 runtime hook 唯一的输入，键和值都是跨仓库契约
        # （docs/container-registration.md）：键必须是 ``sandbox_cgroup``，值必须是
        # **沙盒名**本身 —— 不接受 cgroup 路径、不接受 basename，那些会被
        # 回收复用，沙盒销毁重建后旧 annotation 会指到一个别人的活沙盒上。
        # hook 找不到这个键就什么都不做：容器起得来、但没登记，随后被 BPF 拒。
        #
        # 下面这行是将就写法，别"顺手修": docker-py 7.x 的 HostConfig 没有
        # ``annotations`` 参数，``containers.run`` 也没有注入口（传
        # ``annotations=`` 直接 TypeError）。API 字段是
        # ``HostConfig.Annotations``（不在 Config 顶层），只能拿到
        # ``create_host_config`` 的返回值之后往里塞。
        host_config['Annotations'] = {'sandbox_cgroup': self.sandbox_name}

        try:
            created = client.api.create_container(
                image_ref,
                command=command or None,
                # 不用 --rm: 收尾时自己删，失败也还能 inspect 出状态。
                detach=True,
                environment=environment or None,
                working_dir=self.target.get('workdir') or None,
                user=self.target.get('user') or None,
                labels={
                    # labels 只是 Docker 侧的清理索引（按 label 扫残留容器），
                    # 不会进 OCI bundle，别把它当传参通道。
                    'neu-box.sandbox': self.sandbox_name,
                    'neu-box.sandbox-cgroup': cgroup.path(self.sandbox_name),
                },
                host_config=host_config,
            )
            self._container_id = str(created['Id'])
            client.api.start(self._container_id)
        except DockerExecutorError:
            raise
        except Exception as exc:
            raise DockerExecutorError(
                f'启动容器失败: {exc}', 'docker_run_failed',
            ) from exc
        self.log.write(
            f'[neu-box] 容器 {self._container_id[:12]} 已启动，runtime 负责登记\n'
        )
        return self._container_id

    def _wait_identity(self, client) -> ContainerIdentity | None:
        """等容器 init 就绪并解析身份。

        **返回 ``None`` = 容器在我们看清它之前就已经退出**（短命令的正常结束
        方式）。身份是活着的 init 进程的属性：``container_identity`` 要读
        ``/proc/<pid>``，容器退干净之后没有身份可读，这不是错误，调用方必须
        把 ``None`` 和"拿到身份"分开处理（见 ``_verify_registration``）。
        """
        deadline = time.monotonic() + _REGISTRATION_TIMEOUT
        last_error = None
        while time.monotonic() < deadline:
            try:
                remaining = max(1, int(deadline - time.monotonic()))
                return container_identity(self._container_id, timeout=remaining)
            except DockerExecutorError as exc:
                last_error = exc
                if exc.code == 'docker_container_pid_invalid':
                    # init PID 还没落（Docker 的 State 还没更新）—— 等下一轮。
                    time.sleep(_POLL_INTERVAL)
                    continue
                if exc.code == 'docker_container_not_running':
                    # 容器已经不跑了。再轮询 30 秒只会把真实的退出码换成一句
                    # "未在 30s 内就绪"；启动失败的路径在 ``_start_container``
                    # 就抛了，走到这里只可能是跑完了。
                    return None
                raise
        raise DockerExecutorError(
            f'容器 {self._container_id} 未在 {_REGISTRATION_TIMEOUT:.0f}s 内'
            f'就绪（最后一个错误: {last_error}）',
            'docker_container_not_ready',
        )

    def _verify_registration(self, identity: ContainerIdentity) -> None:
        """复核 runtime 启动钩子已经把这个容器登记到**本**沙盒。

        只允许在身份可读（容器还活着）时调用，这个前提就是本检查的全部
        依据：**读得到身份 ⇒ 记录不可能已经被收走**。收尸线程只在 init
        进程退出后才 ``release_container``（pidfd 可读 = 进程已退出），
        身份读得出来就说明它还没退出，所以此时"查不到记录"只可能是
        "压根没登记过"（runtime 没配好 / hook 没跑），可以 fail-closed。

        反过来，容器已经退出时这条推理不成立 —— 记录可能已经被收走，
        见 ``_run_blocking`` 里 ``identity is None`` 那一支。
        """
        # The OCI runtime hook must have registered this container before
        # the payload was exec'd.  The executor only verifies and adopts
        # that record; it must never perform a late registration after
        # the first NPU initialization window.
        registered = self.sbx.db.get_container(identity.mount_namespace)
        if not registered or registered.get('sandbox_name') != self.sandbox_name:
            raise DockerExecutorError(
                '容器未在 runtime 启动钩子中登记',
                'docker_runtime_not_registered',
            )
        self.log.write(
            f'[neu-box] runtime 归属登记完成'
            f'（mnt ns {identity.mount_namespace}）\n'
        )

    # ── 等待与收尾 ───────────────────────────────────────────────

    def _state(self, client) -> dict:
        try:
            container = client.containers.get(self._container_id)
            container.reload()
            return (container.attrs or {}).get('State') or {}
        except Exception as exc:
            raise DockerExecutorError(
                f'读取容器状态失败: {exc}',
                'docker_container_state_failed',
            ) from exc

    def _kill(self, client):
        try:
            client.containers.get(self._container_id).kill()
        except Exception:
            logger.exception('终止容器 %s 失败', self._container_id)

    def _start_log_stream(self, client):
        """后台跟随容器日志，避免轮询重复写入同一段输出。"""
        def _follow():
            try:
                container = client.containers.get(self._container_id)
                for chunk in container.logs(
                    stdout=True, stderr=True, stream=True, follow=True,
                ):
                    self.log.write(chunk)
            except Exception:
                logger.debug('跟随容器日志结束', exc_info=True)

        thread = threading.Thread(
            target=_follow, daemon=True, name='docker-log-follow',
        )
        thread.start()
        self._log_thread = thread

    def _wait_container(self, client, timeout_seconds: float) -> tuple:
        deadline = (time.monotonic() + timeout_seconds) if timeout_seconds else 0
        while True:
            if self._cancel_event.is_set():
                self._kill(client)
                return -1, False, '用户手动取消'
            state = self._state(client)
            if not state.get('Running'):
                return int(state.get('ExitCode') or 0), False, None
            if deadline and time.monotonic() > deadline:
                self._kill(client)
                return -1, True, f'命令执行超时（{timeout_seconds:.0f} 秒）'
            time.sleep(_POLL_INTERVAL)

    def _release_registration(self):
        """注销归属。失败就留给 reaper —— 它按 starttime + mnt ns 对账。"""
        with self._runtime_lock:
            mount_namespace = self._mount_namespace
            init_pid = self._init_host_pid
            self._mount_namespace = 0
        if not mount_namespace and init_pid:
            try:
                mount_namespace = mount_namespace_of(init_pid)
            except DockerExecutorError:
                return
        if not mount_namespace:
            return
        try:
            self.sbx.release_container(mount_namespace)
            self.log.write('[neu-box] 容器归属已注销\n')
        except Exception:
            logger.exception(
                '注销容器归属失败（mnt ns %s），留给对账回收', mount_namespace,
            )

    def _finish(self, client):
        # Docker's kill/remove calls are asynchronous from the host's point of
        # view.  Do not revoke the mnt-ns authorization while a container
        # process could still hold driver file descriptors.
        stopped = self._ensure_container_stopped(client)
        if stopped:
            self._release_registration()
        if self._log_thread is not None:
            self._log_thread.join(timeout=5)
        if stopped and self._container_id:
            try:
                client.containers.get(self._container_id).remove(force=True)
            except Exception:
                logger.debug('删除容器失败（可能已不存在）', exc_info=True)
        try:
            client.close()
        except Exception:
            logger.exception('关闭 Docker client 失败')

    def _ensure_container_stopped(self, client, timeout: float = 5.0) -> bool:
        """Boundedly wait for Docker to report the container fully stopped."""
        if not self._container_id:
            return True
        deadline = time.monotonic() + timeout
        killed = False
        while True:
            try:
                state = self._state(client)
            except DockerExecutorError:
                return False
            if not state.get('Running'):
                return True
            if not killed:
                self._kill(client)
                killed = True
            if time.monotonic() >= deadline:
                logger.error(
                    '容器 %s 在 %.1fs 内未停止，保留登记交给 reaper',
                    self._container_id, timeout,
                )
                return False
            time.sleep(_POLL_INTERVAL)

    # ── 入口 ─────────────────────────────────────────────────────

    async def run(self, timeout: int | None) -> dict:
        """docker SDK 是阻塞的，丢到线程里跑 —— 对调度层和 host 后端一样。"""
        return await asyncio.to_thread(self._run_blocking, timeout)

    def _run_blocking(self, timeout: int | None) -> dict:
        # 整段（起容器 + 等身份）都跑在 runtime hook 的回调窗口里：hook 会打
        # Worker 自己的 /container/register，那里要拿 SbxManager 的
        # lifecycle_lock / self.lock / registration lock。这条路径上不许持有
        # 它们，否则是 Worker 等自己。收尾阶段才去动 release_container。
        client = docker_client(timeout=_DOCKER_API_TIMEOUT)
        self._client = client
        timeout_seconds = float(timeout) if timeout else 0.0
        try:
            self._start_container(client)
            identity = self._wait_identity(client)
            if identity is not None:
                with self._runtime_lock:
                    self._init_host_pid = identity.init_host_pid
                    self._init_start_time = identity.init_start_time
                    self._mount_namespace = identity.mount_namespace
                self._verify_registration(identity)
            else:
                # 容器跑得比我们看它更快（echo 这类几毫秒的命令）。这一支
                # **不复核登记**，两个观测点都已经合法地清干净了：身份随
                # /proc 消失，记录被 pidfd 收尸线程在容器退出时删掉。在这里
                # 硬判"没登记就失败"就是拿"看不见"当"不存在"。
                #
                # 放弃的只是"报告"，不是保证：
                #   - "hook 早于 payload" 是 runtime 给的：hook 跑在 runc create
                #     里，退非 0 则容器根本起不来（``_start_container`` 会抛）。
                #     容器能跑到退出，本身就说明 hook 成功过。
                #   - 这道检查在 ``start()`` 返回之后才跑，payload 早开始了 ——
                #     它从来只能"报告"，不能"阻止"，对已跑完的容器更无窗口可补。
                #   - runtime 没配好时 BPF 仍然 fail-closed（没有 container_owner
                #     条目 = 没有任何 davinciN 访问权），丢的只是这句报错。
                self.log.write(
                    '[neu-box] 容器在观测前已退出，runtime 登记未复核\n'
                )
                logger.debug(
                    '容器 %s 在解析身份前已退出，跳过登记复核',
                    self._container_id,
                )
            self._start_log_stream(client)

            returncode, timed_out, error = self._wait_container(
                client, timeout_seconds)
            self.log.write(f'[neu-box] 容器退出，returncode={returncode}\n')
            return result(
                returncode=returncode, timed_out=timed_out, error=error,
            )
        except DockerExecutorError as exc:
            self.log.write(f'[neu-box] 容器执行失败: {exc}\n')
            return result(error=str(exc), returncode=-1)
        except Exception as exc:
            logger.exception('容器任务异常')
            return result(error=f'容器执行异常: {exc}', returncode=-1)
        finally:
            self._finish(client)
