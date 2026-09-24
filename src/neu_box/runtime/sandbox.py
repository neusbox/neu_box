"""沙盒生命周期 — 调用 native helper 实现 cgroup v2 + eBPF 设备隔离。

只管"这个沙盒怎么活、怎么死"：create / join / destroy、进程迁回，以及挂在
它名下的容器（归属登记、收容器、pidfd 退出监听）。给谁哪几张卡是
``scheduling/resources.py`` 的事，设备本身是 ``runtime/devices.py`` 的事，
周期收尸在 ``runtime/reaper.py``。
"""

import contextlib
import logging
import os
import select
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import List, Optional

from neu_box.config import sandbox_executable_path
from neu_box.runtime import cgroup, devices
from neu_box.runtime.containers import (
    ContainerIdentity,
    DockerExecutorError,
    docker_client,
    identity_alive,
    load_docker,
    verify_identity,
)
from neu_box.runtime.reaper import Reaper
from neu_box.storage import Database

# 容器退出后等它真的消失的上限。超时说明容器杀不动（D 状态、卡在驱动的
# ioctl 里），这时必须报错让调用方重试，不能装作收干净了 —— 一个还握着
# 设备 fd 的进程会跟着设备预留一起被"释放"到空闲池。
CONTAINER_EXIT_TIMEOUT = 30.0

# `docker stop` 给容器的 SIGTERM 宽限（秒）。到点 Docker 自己会 SIGKILL；还停
# 不下来就再补一次 ``kill``，最终由 ``_await_container_exit`` 判定。
CONTAINER_STOP_GRACE = 10.0

# 启动对账时等一个容器停下来的上限（秒）。启动路径不能在这里无限等：等不到就
# 保留登记，收尸线程下一轮再收（见 ``retire_containers_on_startup``）。
STARTUP_CONTAINER_STOP_TIMEOUT = 15.0

# 容器从 Docker 建出对象到 runtime hook 完成登记的窗口（秒）。窗口内的容器
# 可能正卡在 ``runc create`` 的 hook 上等 Worker 的锁，销毁路径里任何"碰它"
# 的 Docker 操作都可能和它互等：``docker stop`` 要排在容器状态锁后面，而
# 容器（它的 hook）等的正是调用方手里那把锁。所以窗口内先跳过它、把销毁
# 推迟到下一轮，窗口外的残留（崩溃留下的容器）照旧直接停。
#
# 旧代码里这个窗口由内存里的 gate 精确标记（``gates.pending_for_sandbox``，
# 300s TTL）；gate 删除后，容器自己的创建时间是唯一留下来的、跨重启仍然
# 成立的等价线索。60s 远大于一次容器创建，又短到崩溃残留最多多留一两轮收尸。
CONTAINER_START_GRACE = 60.0

# native helper 的 owner 恢复状态目录，对应 ``native/sandbox/src/state.cpp``
# 的 ``kStateDirectory``：每个沙盒一个 ``cgroup_id_<name>`` 文件，里面是它
# 创建时的 cgroup ID。``native list`` 把它算进沙盒清单，``native destroy``
# 靠它按 cgroup ID 清 ``reserved_devices`` —— 它是沙盒在 native 侧的那份
# 记录，路径必须与 native 保持一致。
NATIVE_STATE_DIRECTORY = '/run/neu-box/sandbox-state'
NATIVE_STATE_PREFIX = 'cgroup_id_'


class SandboxAllocationPaused(RuntimeError):
    """Raised when a new sandbox allocation is attempted during maintenance."""

logger = logging.getLogger(__name__)


# ==================================================================
# SbxManager — 沙盒生命周期管理（单例）
# ==================================================================

class SbxManager:
    """Worker 本地沙盒管理器（单例）。

    封装 native helper 的 create / join / destroy / status 调用，
    并在本地 DB 中记录每个沙盒的状态，支持重启后恢复。
    """

    _instance = None
    # Serializes "is this sandbox still live" checks with the writes that
    # depend on them.  The runtime-hook registration path holds it across the
    # state read and the container insert, so ``destroy_sandbox`` cannot tear
    # the sandbox down between the two steps (see docs/container-registration.md).
    _lifecycle_lock = threading.RLock()

    def __init__(self):
        # native/ 构建出来的特权 helper 的绝对路径。
        self._native_path = str(sandbox_executable_path())
        self._allocations_paused = False
        self._allocations_in_flight = 0

        # 本地 DB（统一 SQLite）
        self.db = Database.get_instance()

        # 线程安全
        self.lock = threading.RLock()
        # Registration is a compare-and-register transaction.  Callers must
        # not perform their ``get_container`` check separately: two
        # registration requests can otherwise both pass the check and
        # INSERT OR REPLACE each other's mount-namespace row.
        self._container_registration_lock = threading.RLock()

        # 容器退出监听: mnt ns -> (mnt ns fd, pid fd)。
        #   mnt ns fd 是"钉住"：只要登记还在，这个 namespace 就被我们引用
        #   着，内核不会回收它，inum 也就发不出去 —— 容器退出后残留的登记
        #   不会让后来的新容器白捡一份授权。
        #   pid fd 是"什么时候死"：容器一退出它就变可读。
        # 两个 fd 都由本进程持有（native helper 是一次性进程，持不住），
        # 收尸线程阻塞在 _epoll 上等它们。
        self._container_fds: dict = {}
        self._epoll = select.epoll()

        # 收尸器：启动恢复 + 周期扫描（状态仍在本对象上）
        self.reaper = Reaper(self)
        self.reaper.recover_on_startup()
        # 容器归属的兜底对账。BPF 还没加载时 native 会拒绝，记录保留给
        # 收尸线程重试（此时 map 是空的，不存在误授权的窗口）。
        self.reconcile_containers()
        # 崩溃重启不续授权：活着的登记容器一律停掉（不删，可写层留着）。
        self.retire_containers_on_startup()

    @classmethod
    def get_instance(cls) -> 'SbxManager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def lifecycle_lock(cls):
        return cls._lifecycle_lock

    # ── 内部工具 ─────────────────────────────────────────────────

    def _device_major(self) -> int | None:
        """Return the major shared by managed device nodes, if any."""
        majors = {int(device.split(':', 1)[0])
                  for device in devices.discover_nodes()}
        if not majors:
            return None
        if len(majors) > 1:
            raise RuntimeError(
                f'受管设备包含多个 major，native sandbox 不支持: {sorted(majors)}')
        return next(iter(majors))

    @staticmethod
    def _cgroup_of(pid: int) -> str:
        """读 /proc/<pid>/cgroup 的 cgroup v2 路径；读不到返回空串。

        空串表示"没有 origin"，release 时这个进程会被当成沙盒里长出来的
        一起收掉 —— fail-closed，不会因为读不到就把来路不明的东西放出去。
        """
        try:
            with open(f'/proc/{int(pid)}/cgroup', encoding='utf-8') as stream:
                for line in stream:
                    hierarchy, _controllers, path = line.rstrip('\n').split(
                        ':', 2,
                    )
                    if hierarchy == '0':
                        return path.rstrip('/') or '/'
        except (OSError, ValueError) as exc:
            logger.warning('读取 PID %s 的 cgroup 失败: %s', pid, exc)
        return ''

    def _run_native(self, *args) -> subprocess.CompletedProcess:
        """调用 native sandbox helper，返回 CompletedProcess。"""
        cmd = [self._native_path]
        # The helper discovers the BPF object beside itself in the release
        # layout.  Supplying it explicitly also makes configured/dev paths
        # deterministic and avoids depending on the current working directory.
        object_path = os.path.join(os.path.dirname(self._native_path), 'device_block.o')
        if os.path.isfile(object_path):
            cmd.extend(['--bpf-object', object_path])
        cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    def load_bpf(self) -> bool:
        """Ensure the native BPF program is loaded for managed devices."""
        major = self._device_major()
        # CPU-only installations have no device filter; native helper cannot
        # load a device program without a major and has nothing to enforce.
        if major is None:
            return True
        result = self._run_native('--device-major', str(major), 'load')
        if result.returncode != 0:
            raise RuntimeError(f'加载 sandbox BPF 失败: {result.stderr.strip()}')
        return True

    def set_allocations_paused(self, paused: bool) -> None:
        with self.lock:
            self._allocations_paused = bool(paused)

    def allocations_paused(self) -> bool:
        with self.lock:
            return self._allocations_paused

    @contextlib.contextmanager
    def allocation_guard(self):
        """分配期间的维护门：暂停时拒绝，并计入 in-flight（维护要等它归零）。

        由 ``scheduling.resources.allocate`` 包住"选设备 + 建沙盒"整段 ——
        pause 要么看到 in-flight 还没落到 running 的任务，要么等它归零。
        """
        with self.lock:
            if self._allocations_paused:
                raise SandboxAllocationPaused(
                    'Worker 处于暂停维护状态，暂不创建新沙盒')
            self._allocations_in_flight += 1
        try:
            yield
        finally:
            with self.lock:
                self._allocations_in_flight = max(
                    0, self._allocations_in_flight - 1)

    def allocation_status(self) -> dict:
        with self.lock:
            return {
                'paused': self._allocations_paused,
                'in_flight': self._allocations_in_flight,
            }

    def lifecycle_status(self) -> dict:
        """Summarize persisted and filesystem sandbox state for maintenance."""
        db_names = set(self.list_sandboxes())
        # Maintenance must still be able to reach the cleanup step when the
        # old pinned BPF object cannot pass the new helper's strict ABI/tag
        # validation.  The non-strict listing falls back to cgroup discovery
        # and therefore never hides a live sandbox; cleanup then decides
        # whether the pins can be safely detached.
        fs_names = set(self.list_sandboxes_via_native(strict=False))
        residuals = sorted(db_names | fs_names)
        states = {record['name']: record.get('state')
                  for record in self.db.list_sandboxes()}
        return {
            'active': sum(state == 'ACTIVE' for state in states.values()),
            'creating': sum(state == 'CREATING' for state in states.values()),
            'destroying': sum(state == 'DESTROYING' for state in states.values()),
            'residuals': residuals,
        }

    @staticmethod
    def _cg_path(name: str) -> str:
        return cgroup.path(name)

    @staticmethod
    def _native_state_path(name: str) -> str:
        return os.path.join(NATIVE_STATE_DIRECTORY, NATIVE_STATE_PREFIX + name)

    def _native_state_exists(self, name: str) -> bool:
        """native 侧还留着这个沙盒的 owner 状态文件吗？

        沙盒有三份记录：DB 行、cgroup 目录、native 的 owner 状态文件。前两
        份都不在时不能直接认定"它本来就没了" —— 状态文件还在就说明
        ``reserved_devices`` 里可能还挂着它的设备预留，而 ``native list``
        会把状态文件算进沙盒清单（收尸因此会反复看见这份残留）。
        用 ``lexists``：native 侧的 ``state_exists`` 就是 ``symlink_status``
        语义，悬空符号链接也算"在"。
        """
        return os.path.lexists(self._native_state_path(name))

    @staticmethod
    def _sandbox_generation(record: Optional[dict]):
        """沙盒行的"代次"：同名沙盒被销毁后重建会拿到一个新值。

        销毁路径中间有一段不持锁（收容器），期间同名沙盒可能已经被别的
        销毁流程放掉、又被 ``create_sandbox`` 重建。重建出来的不是刚才决定
        销毁的那一个，不能再替它做 native 清理。``created_at`` 是
        ``insert_sandbox`` 用 ``time.time()`` 落的，重建必然换值。
        """
        return None if record is None else record.get('created_at')

    # ── 核心操作 ─────────────────────────────────────────────────

    def create_sandbox(self, name: str, cpu: int = 0, mem: str = "0",
                       devices: Optional[List[str]] = None) -> bool:
        """创建沙盒。

        Args:
            name:    沙盒名称
            cpu:     CPU 核数 (0=不限)
            mem:     内存限制 (如 "512M", "2G", "0"=不限)
            devices: 设备号列表 (如 ["235:0", "235:1"])。None 表示不预留任何设备。

        Returns:
            True 表示创建成功（或已存在且有效）。
        """
        with self.lock:
            existing = self.db.get_sandbox(name)
            # 已存在且在 cgroup 中有效 → 直接返回。CREATING 是启动恢复
            # 的可接管状态，不能被第二次 create 覆盖。
            if os.path.isdir(self._cg_path(name)):
                if existing:
                    if existing.get('state') == 'DESTROYING':
                        # A previous destroy may have been interrupted after
                        # killing processes but before native cleanup. Never
                        # hand this cgroup back to a new task while its BPF
                        # reservation is still being reconciled.
                        logger.warning(
                            "沙盒 '%s' 正在销毁，拒绝复用并等待 Reaper",
                            name,
                        )
                        return False
                    logger.warning("沙盒 '%s' 已存在，跳过创建", name)
                    return True
                # A cgroup without a DB row is an external/orphaned object;
                # reusing its name could attach a task to stale restrictions.
                logger.warning("沙盒 '%s' 存在但没有 DB 记录，拒绝复用", name)
                return False
            if existing:
                # The cgroup may have been removed externally while its DB row
                # and BPF reservation survived. Reconcile that stale row
                # before inserting a new row with the deterministic name.
                if existing.get('state') == 'DESTROYING':
                    logger.warning(
                        "沙盒 '%s' 已在销毁，拒绝复用并等待 Reaper", name,
                    )
                    return False
                if not self.destroy_sandbox(name):
                    logger.warning(
                        "沙盒 '%s' 的旧记录无法清理，拒绝复用", name,
                    )
                    return False

            # 构建命令行
            args = ['create', name, str(cpu), mem]
            if devices:
                args.extend(devices)

            # 先持久化 intent，再调用 native helper。这样进程若在 create
            # 期间崩溃，启动恢复可区分半创建 sandbox 并接管或回收它。
            self.db.insert_sandbox(
                name=name, cpu=cpu, mem=mem,
                devices=devices or [],
                cgroup_path=self._cg_path(name),
                pids=[])
            logger.warning("创建沙盒 '%s' (cpu=%s, mem=%s, devices=%s)", name, cpu, mem, devices)
            # 库行已经落库：从这里往下无论怎么失败都必须走回滚，否则那行会
            # 带着 devices 留在 DB 里，`resources.allocated_devices()` 会把
            # 这几张卡一直算成已占用，直到进程重启。
            # Native helper requires a device major even for CPU-only
            # sandboxes; major 0 simply matches no managed device.
            try:
                major = self._device_major()
                result = self._run_native(
                    '--device-major', str(major if major is not None else 0),
                    *args,
                )
                failed = result.returncode != 0
                detail = result.stderr.strip() if failed else ''
            except Exception as exc:
                failed, detail = True, str(exc)
            if failed:
                logger.error("创建沙盒 '%s' 失败: %s", name, detail)
                # Keep cleanup best-effort. destroy_sandbox marks DESTROYING
                # and retains the device metadata when native cleanup fails,
                # allowing the reaper to retry even if a child survived.
                try:
                    self.destroy_sandbox(name)
                except Exception:
                    logger.exception("创建失败后的 sandbox '%s' 回收异常", name)
                return False

            # Keep CREATING until the first join.  The command/container
            # startup can be slow; activation is the join operation's
            # responsibility and the Reaper skips this transitional state.
            logger.warning("✓ 沙盒 '%s' native 创建成功，等待首次 join", name)
            return True

    def join_sandbox(self, name: str, pid: int,
                     borrowed: bool = False) -> bool:
        """将进程加入沙盒。

        ``borrowed`` 表示这个进程是"从外面借进来的"（acquire 传进来的那个
        shell）：release 时它会被迁回原来的 cgroup。默认 False —— Worker 自
        己起的命令进程属于沙盒里长出来的东西，跟着沙盒一起收掉才对。

        Returns:
            True 表示加入成功。
        """
        with self.lock:
            record = self.db.get_sandbox(name)
            if not record:
                logger.error("加入失败: 沙盒 '%s' 不在 DB 中", name)
                return False
            if record.get('state') == 'DESTROYING':
                logger.error("加入失败: 沙盒 '%s' 正在销毁", name)
                return False

            # 先记下它原来在哪个 cgroup。join 之后 /proc/<pid>/cgroup 只剩
            # 沙盒路径，release 就是靠这条记录把"借来的"进程还回去的。
            origin = self._cgroup_of(pid) if borrowed else ''

            result = self._run_native('join', name, str(pid))
            if result.returncode != 0:
                logger.error("加入 PID %s 到 '%s' 失败: %s", pid, name, result.stderr.strip())
                return False
            if origin:
                self.db.set_sandbox_origin(name, pid, origin)

            # DB 只保存 cgroup.procs 的当前快照，不累积历史 PID。读取
            # 失败时至少用本次已成功加入的 PID 覆盖旧快照，Reaper 会在
            # 下一轮重新同步；DB 从不参与存活判断。
            try:
                snapshot = self._read_cgroup_snapshot(name)
                pids = snapshot[0] if snapshot is not None else []
            except OSError as exc:
                logger.warning(
                    "读取沙盒 '%s' PID 快照失败，暂存 PID %s: %s",
                    name, pid, exc,
                )
                pids = [pid]
            self.db.update_sandbox_pids(name, pids)
            self.db.prune_sandbox_origins(name, pids)
            activate = getattr(self.db, 'activate_sandbox', None)
            # Existing ACTIVE sandboxes support the public /join operation;
            # only the first join of a CREATING sandbox performs the lifecycle
            # transition. Calling activate on ACTIVE would report rowcount=0
            # and incorrectly turn a successful join into an error.
            if (record.get('state') == 'CREATING' and activate is not None
                    and not activate(name, pids)):
                logger.error("加入成功但 sandbox '%s' 无法切换为 ACTIVE", name)
                return False

            logger.warning("✓ PID %s 已加入沙盒 '%s'", pid, name)
            return True

    def destroy_sandbox(self, name: str) -> bool:
        """销毁沙盒，清理 cgroup 和 eBPF 预留，释放设备。

        Returns:
            True 表示销毁成功（或沙盒本来就不存在）。
        """
        # Reaper scans while holding ``self.lock`` and may call this method
        # reentrantly. Keep the lock order self.lock → lifecycle lock so that
        # teardown cannot deadlock with that scan.
        with self.lock, self._lifecycle_lock:
            record = self.db.get_sandbox(name)
            # 沙盒有三份记录：DB 行、cgroup 目录、native 的 owner 状态文件。
            # 三份都不在才能认定"本来就不存在"：只看前两份时，"没有 DB 行、
            # 没有 cgroup 目录、但状态文件还在"的残留会被当成销毁成功直接
            # 返回，native destroy 根本不会被调用 —— 那个文件里的设备预留
            # 永远留着，而 ``native list`` 又把它算进沙盒清单，收尸于是反复
            # 看见它、反复"清干净"，那张卡一直算被占着。
            if (not os.path.isdir(self._cg_path(name)) and not record
                    and not self._native_state_exists(name)):
                return True

            # 先落 DESTROYING 标记再动容器：收容器失败时（容器杀不动）要能
            # 靠它被周期收尸重试 —— CREATING/ACTIVE 的沙盒不在收尸的扫描
            # 范围里，标记晚了这条路径就没人再碰，设备预留会一直挂着。
            #
            # 这个标记同时也是下面"出临界区收容器"的护栏：登记路径在锁里
            # 读到的状态就是 DESTROYING，插不进来（见
            # ``register_runtime_container`` 的状态表）。
            marker = getattr(self.db, 'mark_sandbox_destroying', None)
            if marker is not None:
                marker(name)

        # ── 收容器：**出临界区** ──────────────────────────────────
        #
        # 收容器放在 destroy 而不是 release 的分支里：submit 容器的自动收尾
        # 走的也是这条 destroy 路径，分开写会漏一条。失败就原样保留沙盒等
        # 重试，绝不带着一个还活着的容器往下走。
        #
        # 它必须留在锁外：这段会真的等容器（``docker stop`` 排在 Docker 的
        # 容器状态锁后面，之后还要等 init 真的退出），而正在 ``runc create``
        # 里的那个容器的 runtime hook 要拿 ``self.lock → lifecycle_lock``
        # 才能登记 —— 持着锁等它就是"销毁等容器、容器等锁"的环。放锁外之后
        # hook 永远拿得到锁，拿到的结论是 DESTROYING（被拒）→ runc create
        # 失败、容器根本起不来 → 销毁等的这次退出也就有了着落。
        #
        # 不可能有新的登记在"标记之后、收容器之前"钻进来：标记是在同一个
        # 临界区里落的，而登记路径只认 DESTROYING 这一种状态就拒。
        try:
            self._retire_containers_of(name)
        except DockerExecutorError as exc:
            logger.error(
                "销毁沙盒 '%s' 前收容器失败（沙盒保持 DESTROYING 待重试）: %s",
                name, exc,
            )
            return False

        with self.lock, self._lifecycle_lock:
            # 出锁这段时间名字可能已经被别的销毁流程放掉、又被 create 重建。
            # 重建出来的沙盒不是刚才决定销毁的那一个，绝不能替它做 native
            # destroy —— 那会拆掉一个活着的沙盒。
            if (self._sandbox_generation(self.db.get_sandbox(name))
                    != self._sandbox_generation(record)):
                logger.warning(
                    "沙盒 '%s' 在收容器期间已被销毁或重建，跳过 native 清理",
                    name,
                )
                return True

            # 归还借来的进程（只对记了 origin 的 PID 生效），必须在 native
            # destroy 杀掉 cgroup 里其余进程之前做。
            self._return_borrowed_pids(name)

            # 即使 cgroup 目录已经消失也必须调用脚本：脚本会根据持久化
            # 的设备清单删除 eBPF reserved_devices 条目。只有脚本成功且
            # cgroup 确认消失后才能删 DB，否则保留记录供 Reaper 重试。
            try:
                result = self._run_native('destroy', name)
            except (OSError, subprocess.SubprocessError) as exc:
                # 单个沙盒脚本超时/启动失败不能打断整个 Reaper 扫描，
                # 保留 DB 和设备元数据供下一轮继续尝试。
                logger.error("销毁沙盒 '%s' 执行失败: %s", name, exc)
                return False
            if result.returncode != 0:
                logger.error("销毁沙盒 '%s' 失败: %s", name, result.stderr.strip())
                return False

            # 脚本返回成功 ≠ cgroup 目录已消失（systemd slice 可能阻止 rmdir）
            if os.path.isdir(self._cg_path(name)):
                logger.warning("沙盒 '%s' cgroup 目录未清除，保留 DB 记录等待重试", name)
                return False

            # native destroy 会按 cgroup ID 一并清掉容器归属的 BPF 条目，
            # 这里删掉对应的记录，两边不留下"账在、map 不在"的分叉。
            self.db.delete_containers_of_sandbox(name)
            self.db.delete_sandbox(name)
            logger.warning("✓ 沙盒 '%s' 已销毁", name)
            return True

    # ── 容器归属登记 ─────────────────────────────────────────────
    #
    # 容器不是沙盒：它不进沙盒 cgroup，也不持有授权。沙盒把自己的那份授权
    # 委托给容器用，判定依据是 container_owner map 里的登记，而登记必须在
    # 容器里第一个 NPU 进程之前写完 —— 驱动的 UDA 设备表在第一次 NPU 初始
    # 化时按当时权限建出来，之后按 mnt ns 缓存复用，晚了就再也补不回来。

    def bind_container(self, sandbox_name: str, mount_namespace: int) -> None:
        """把容器的 mount namespace 登记到沙盒的授权上（写 BPF map）。

        入参是 inum 而不是 PID：调用方已经为这个 namespace 开了 fd（pin），
        native 侧再 stat 一次反而可能因为容器刚好退出而失败。
        """
        result = self._run_native(
            'bind-container', sandbox_name, str(int(mount_namespace)))
        if result.returncode != 0:
            raise DockerExecutorError(
                f'登记容器 mnt ns {mount_namespace} 到沙盒 '
                f'{sandbox_name} 失败: {result.stderr.strip()}',
                'sandbox_container_bind_failed',
            )

    def unbind_container(self, mount_namespace: int) -> None:
        """删除一条容器归属登记（幂等）。"""
        if not mount_namespace:
            return
        result = self._run_native('unbind-container', str(int(mount_namespace)))
        if result.returncode != 0:
            raise DockerExecutorError(
                f'删除容器登记 mnt ns {mount_namespace} 失败: '
                f'{result.stderr.strip()}',
                'sandbox_container_unbind_failed',
            )

    def open_container_handles(self, init_host_pid: int) -> tuple:
        """开两个 fd: mnt ns fd（pin）和 pidfd（监听退出）。

        返回 ``(mnt_fd, mount_namespace, pid_fd)``。``fstat(mnt_fd).st_ino``
        就是 BPF 读到的 ``ns.inum`` —— 拿号和钉住是同一个动作。

        必须在写 BPF map **之前**调用：容器可能在这两步之间自己退出，那时
        就再也 open 不到它的 namespace 了。反过来先钉住再登记，最坏的结果
        只是登记不成立（容器已经不在），不会留下一条没有 pin 保护的授权。
        """
        try:
            mnt_fd = os.open(f'/proc/{int(init_host_pid)}/ns/mnt', os.O_RDONLY)
        except OSError as exc:
            raise DockerExecutorError(
                f'无法打开容器 PID {init_host_pid} 的 mount namespace: {exc}',
                'docker_container_pid_invalid',
            ) from exc
        try:
            mount_namespace = os.fstat(mnt_fd).st_ino
            pid_fd = os.pidfd_open(int(init_host_pid))
        except OSError as exc:
            os.close(mnt_fd)
            raise DockerExecutorError(
                f'无法监听容器 PID {init_host_pid}: {exc}',
                'docker_container_pid_invalid',
            ) from exc
        return mnt_fd, mount_namespace, pid_fd

    def register_container(self, sandbox_name: str,
                           identity: ContainerIdentity) -> dict:
        """登记容器归属: 钉住 namespace → 写 BPF map → 落库 → 挂监听。

        调用方只能在它返回之后放行容器。顺序是正确性的一部分（见
        ``open_container_handles``）；落库失败就撤销 map 登记，宁可容器
        一直停着，也不能留下"map 有、账没有"的孤儿授权。
        """
        mnt_fd, mount_namespace, pid_fd = self.open_container_handles(
            identity.init_host_pid)
        try:
            verify_identity(identity)
            if mount_namespace != identity.mount_namespace:
                raise DockerExecutorError(
                    f'容器 {identity.container_ref} 的 mount namespace 在登记'
                    f'前后发生变化: {mount_namespace} != '
                    f'{identity.mount_namespace}',
                    'docker_container_changed',
                )
            self.bind_container(sandbox_name, mount_namespace)
        except Exception:
            os.close(pid_fd)
            os.close(mnt_fd)
            raise

        record = {
            'mount_namespace': mount_namespace,
            'container_ref': identity.container_ref,
            'container_id': identity.container_id,
            'init_host_pid': identity.init_host_pid,
            'init_start_time': identity.init_start_time,
            'sandbox_name': sandbox_name,
        }
        try:
            self.db.insert_container(**record)
        except Exception:
            logger.exception('容器归属落库失败，撤销 BPF 登记')
            self.unbind_container(mount_namespace)
            os.close(pid_fd)
            os.close(mnt_fd)
            raise
        with self.lock:
            # 同一个容器重复登记时换掉旧 fd，不能把它们泄漏在 epoll 里。
            previous = self._container_fds.pop(int(mount_namespace), None)
            if previous is not None:
                try:
                    self._epoll.unregister(previous[1])
                except OSError:
                    pass
                os.close(previous[1])
                os.close(previous[0])
            self._container_fds[int(mount_namespace)] = (mnt_fd, pid_fd)
            self._epoll.register(pid_fd)
        logger.warning(
            "容器 %s (PID %s, mnt ns %s) 已登记到沙盒 '%s'",
            identity.container_ref, identity.init_host_pid,
            mount_namespace, sandbox_name,
        )
        return record

    def register_container_if_free(self, sandbox_name: str,
                                   identity: ContainerIdentity) -> dict:
        """Atomically reject an existing namespace and register a container.

        ``storage.insert_container`` deliberately uses ``INSERT OR REPLACE``
        for recovery tooling, so callers that need exclusivity must serialize
        the existence check with the write.  This is the transactional core of
        ``register_runtime_container``; the lower-level ``register_container``
        remains useful for startup reconciliation and tests.
        """
        # ``destroy_sandbox`` decides under ``self.lock``/``lifecycle_lock``
        # and only then retires containers with those locks released.  Keeping
        # the same ``self.lock``-first order everywhere is what makes the two
        # paths unable to wait on each other.
        with self.lock, self._container_registration_lock:
            sandbox = self.db.get_sandbox(sandbox_name)
            if not sandbox or sandbox.get('state') == 'DESTROYING':
                raise DockerExecutorError(
                    f'沙盒 {sandbox_name} 当前不可用',
                    'sandbox_not_active',
                )
            existing = self.db.get_container(identity.mount_namespace)
            if existing is not None:
                code = ('docker_container_registered_elsewhere'
                        if existing.get('sandbox_name') != sandbox_name
                        else 'docker_container_already_registered')
                raise DockerExecutorError(
                    f'容器 mnt ns {identity.mount_namespace} 已登记在沙盒 '
                    f'{existing.get("sandbox_name")}', code)
            return self.register_container(sandbox_name, identity)

    def register_runtime_container(self, sandbox_name: str,
                                   identity: ContainerIdentity) -> tuple[dict, bool]:
        """登记 OCI runtime hook 报上来的容器；返回 ``(记录, 是否新建)``。

        ``docs/container-registration.md`` 要求「读沙盒状态」和「写登记」在同一个
        ``lifecycle_lock`` 临界区里 —— 否则 ``destroy_sandbox`` 能在两步之间
        把沙盒拆掉，登记就落到一个正在消失的授权上。

        状态语义（同一份契约）：``DESTROYING`` 拒，``ACTIVE`` 直接用，
        ``CREATING`` **登记即 join** —— 先推成 ACTIVE 再登记。docker 命令任务
        的容器按设计不搬进沙盒 cgroup，永远不会调 ``join_sandbox``，沙盒会
        一直停在 CREATING（见 ``create_sandbox`` 末尾的注释）；在这里把
        CREATING 拒掉等于每个 docker 命令任务都起不来。

        锁序固定为 ``self.lock → lifecycle_lock → registration lock``：
        ``destroy_sandbox`` 是 ``self.lock → lifecycle_lock``，
        ``register_container_if_free`` / ``release_container`` 是
        ``self.lock → registration lock``。**不能反过来先拿
        ``lifecycle_lock`` 再去拿 ``self.lock``** —— 那会和 destroy 路径
        互相等待。

        destroy 只在"定状态 + 落标记 + 改台账"的时候握锁，收容器（会等
        Docker 和容器退出）在锁外做（见 ``destroy_sandbox``），所以这里
        不会和"销毁等容器"的等待叠在一起。
        """
        with self.lock, self._lifecycle_lock, self._container_registration_lock:
            sandbox = self.db.get_sandbox(sandbox_name)
            if not sandbox:
                raise DockerExecutorError(
                    f'沙盒 {sandbox_name} 不存在',
                    'sandbox_not_found',
                )
            if sandbox.get('state') == 'DESTROYING':
                raise DockerExecutorError(
                    f'沙盒 {sandbox_name} 当前不可用',
                    'sandbox_not_active',
                )
            if sandbox.get('state') == 'CREATING':
                # 与登记在同一个临界区里，destroy 插不进来。
                self._promote_on_registration(sandbox_name, sandbox)
            existing = self.db.get_container(identity.mount_namespace)
            if existing is not None:
                if existing.get('container_id') == identity.container_id:
                    # 同一个容器在这个 mnt ns 上的重复登记（hook 在 exec 上也报到
                    # 同一个 ns）：**以既有绑定为准**，不跟 annotation 走 ——
                    # `neubox docker start` 按借条改绑之后，容器里再 exec 时
                    # annotation 里还是建容器时那个沙盒名。
                    if existing.get('sandbox_name') != sandbox_name:
                        logger.warning(
                            "容器 %s（mnt ns %s）已绑在沙盒 '%s'，"
                            "忽略本次登记报上来的 '%s'",
                            identity.container_ref, identity.mount_namespace,
                            existing.get('sandbox_name'), sandbox_name,
                        )
                    return existing, False
                raise DockerExecutorError(
                    f'容器 mnt ns {identity.mount_namespace} 已登记在沙盒 '
                    f'{existing.get("sandbox_name")}',
                    'docker_container_registered_elsewhere',
                )
            return self.register_container_if_free(
                sandbox_name, identity), True

    def _promote_on_registration(self, sandbox_name: str, sandbox: dict) -> None:
        """把 CREATING 的沙盒推成 ACTIVE：登记即 join。调用方必须已持有锁。

        容器不在沙盒 cgroup 里，所以容器 init PID **不进** pids ——
        ``pids`` 记的是"谁在沙盒 cgroup 里"，这里只沿用记录里的快照
        （CREATING 途中的沙盒本就没有成员），不凭空造也不抹掉。
        """
        pids = list(sandbox.get('pids') or [])
        if self.db.activate_sandbox(sandbox_name, pids):
            logger.warning(
                "沙盒 '%s' 由容器登记推入 ACTIVE（登记即 join）", sandbox_name,
            )
            return
        # rowcount=0：状态在我们读它之后被改过。join 也拿 ``self.lock``，
        # 正常不会发生；只认"已经是 ACTIVE"，其余按不可用拒绝（fail-closed）。
        current = self.db.get_sandbox(sandbox_name) or {}
        if current.get('state') == 'ACTIVE':
            return
        raise DockerExecutorError(
            f'沙盒 {sandbox_name} 当前不可用',
            'sandbox_not_active',
        )

    def _revoke_container(self, record: dict) -> None:
        """撤掉 BPF 里那条授权（幂等）。记录和 pin 都留着。"""
        self.unbind_container(int(record['mount_namespace']))

    def _drop_container_registration(self, record: dict) -> None:
        """撤授权 + 删记录。fd 不在这里关 —— pin 要等登记真的没了再放。"""
        mount_namespace = int(record['mount_namespace'])
        self.unbind_container(mount_namespace)
        self.db.delete_container(mount_namespace)

    def _close_container_handles(self, mount_namespace: int) -> None:
        """释放 pin 和监听 fd。必须在上面的删记录之后调用。"""
        with self.lock:
            handles = self._container_fds.pop(int(mount_namespace), None)
        if handles is None:
            return
        mnt_fd, pid_fd = handles
        try:
            self._epoll.unregister(pid_fd)
        except OSError:
            pass
        os.close(pid_fd)
        os.close(mnt_fd)

    def release_container(self, mount_namespace: int, *,
                          expected_sandbox_name: str | None = None,
                          expected_container_id: str | None = None,
                          expected_init_start_time: int | None = None) -> None:
        """Atomically validate ownership, revoke authorization, and unpin."""
        # Keep the same ``self.lock → registration lock`` order as
        # ``register_container_if_free`` and ``destroy_sandbox``.
        with self.lock, self._container_registration_lock:
            self._release_container_locked(
                mount_namespace,
                expected_sandbox_name=expected_sandbox_name,
                expected_container_id=expected_container_id,
                expected_init_start_time=expected_init_start_time,
            )

    def _release_container_locked(self, mount_namespace: int, *,
                                  expected_sandbox_name: str | None = None,
                                  expected_container_id: str | None = None,
                                  expected_init_start_time: int | None = None) -> None:
        """注销容器归属: 撤 map → 删记录 → 放 pin，顺序不能反。

        只要登记还在，那个 mnt ns 就被我们引用着，内核不会回收它、inum
        也就发不出去 —— 所以这里没有"登记已删、号还能被别人捡走"的窗口。
        """
        if not mount_namespace:
            return
        record = self.db.get_container(mount_namespace)
        if record is not None:
            # A late rollback can arrive after the mount namespace key was
            # reused by a newer container.  Never let it blindly delete a
            # registration it no longer owns.
            if (expected_sandbox_name is not None
                    and record.get('sandbox_name') != expected_sandbox_name):
                return
            if (expected_container_id is not None
                    and record.get('container_id') != expected_container_id):
                return
            if (expected_init_start_time is not None
                    and int(record.get('init_start_time') or 0)
                    != int(expected_init_start_time)):
                return
            # 删记录永远在撤 map 之后（见 _drop_container_registration），
            # 所以"记录已经不在"就等于"map 已经撤过"。这里再按 inum 撤一次
            # 是盲撤：那一瞬间它可能已经被内核分给了新容器，撤掉就是误删
            # 别人的授权。
            self._drop_container_registration(record)
        self._close_container_handles(mount_namespace)

    @staticmethod
    def _fd_readable(fd: int) -> bool:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        return bool(poller.poll(0))

    def _container_alive(self, record: dict) -> bool:
        """容器还活着吗？有 pidfd 就直接问它，比读 /proc 更准。"""
        handles = self._container_fds.get(int(record['mount_namespace']))
        if handles is not None:
            # pidfd 可读 = 进程已退出；它钉的是 struct pid，不受 PID 复用影响。
            return not self._fd_readable(handles[1])
        return identity_alive(
            init_host_pid=record['init_host_pid'],
            init_start_time=record['init_start_time'],
            mount_namespace=record['mount_namespace'],
        )

    def containers_of(self, sandbox_name: str,
                      alive_only: bool = False) -> List[dict]:
        """沙盒名下的容器记录。``alive_only`` 时只返回仍然存活的。"""
        records = self.db.list_containers(sandbox_name)
        if not alive_only:
            return records
        return [record for record in records if self._container_alive(record)]

    # ── 收容器（release / destroy 共用） ─────────────────────────

    def _stop_container(self, container_ref: str) -> bool:
        """停掉一个容器；**不删**，容器已经不在时静默返回。

        为什么不 ``docker rm -f``：删容器会连它的可写层一起销毁，而用户很可能
        还要 ``docker commit`` / ``docker cp`` 把里面的产物捞出来。我们要的只是
        "它的进程别再占着卡"，停掉就够了 —— 进程一没，那个 mount namespace 就
        死了，驱动按 mnt ns 缓存的那张 UDA 表也就没人能用（见
        ``docs/isolation.md``）。容器留着，下次 ``docker start`` 会重新走一遍
        runtime hook，登记被拒就起不来（fail-closed），不会带着旧授权复活。

        先 SIGTERM（``stop``，到 ``CONTAINER_STOP_GRACE`` 后 Docker 自己
        SIGKILL），停不下来再补一次 ``kill``。这里不判定"真的停了"——
        ``_await_container_exit`` 才是判据，报错也要由它抛出去。

        返回 ``True`` 表示"这条 Docker 操作完成了"（含容器已经不存在），
        ``False`` 表示连 Docker 都没问到 —— 调用方据此决定要不要把这一轮
        算作"没扫干净"。
        """
        try:
            client = docker_client(timeout=10)
        except DockerExecutorError as exc:
            logger.error('停容器 %s 失败（docker 不可用）: %s', container_ref, exc)
            return False
        # docker_client 已经成功说明 docker 装了，这里取的是异常类型本身。
        not_found = load_docker().errors.NotFound
        try:
            container = client.containers.get(container_ref)
        except not_found:
            return True
        except Exception:
            logger.warning('取容器 %s 失败', container_ref, exc_info=True)
            return False
        try:
            try:
                container.stop(timeout=CONTAINER_STOP_GRACE)
            except not_found:
                # 容器跑完退出之后 Docker 自己会把它清掉，收尸时再来看就是
                # 404。这是正常路径，不能和下面那条 catch-all 合并 —— 一条
                # 404 打两段 traceback 会把真正的失败埋掉。
                return True
            except Exception:
                logger.warning(
                    '停容器 %s 失败，改用 kill', container_ref, exc_info=True,
                )
                try:
                    container.kill()
                except not_found:
                    return True
                except Exception:
                    logger.warning('kill 容器 %s 失败', container_ref, exc_info=True)
                    return False
            return True
        finally:
            try:
                client.close()
            except Exception:
                logger.debug('关闭 Docker client 失败', exc_info=True)

    @staticmethod
    def _parse_docker_timestamp(value) -> Optional[float]:
        """Docker 的 RFC3339 时间戳 → epoch 秒；读不出来返回 ``None``。

        Docker 给的是纳秒精度的 UTC（``2026-09-14T10:00:00.123456789Z``）。
        本项目的 ``requires-python`` 是 ``>=3.11``，而 3.11 起的
        ``fromisoformat`` 直接吃得下任意位小数和 ``Z``（3.10 及以前只吃 3/6
        位并且不认 ``Z``），所以这里不需要自己剪裁。

        唯一要补的是"串里没有时区"这一种：``timestamp()`` 会按**本地**时区
        解释裸时间，而 Docker 的时间戳都是 UTC —— 在 UTC+8 的机器上算出来会
        差 8 小时，让刚建出来的容器看起来"很老"，正好从启动窗口里漏出去。
        """
        try:
            parsed = datetime.fromisoformat(str(value or ''))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()

    def _container_starting(self, container) -> bool:
        """这个 Docker 容器是否还在"创建 → 登记"的窗口里？

        窗口内的容器可能正卡在 ``runc create`` 的 hook 上等 Worker 的锁
        （``POST /container/register`` 要 ``self.lock → lifecycle_lock``），
        所以销毁路径**不能**碰它：``docker stop`` 要排在容器的状态锁后面，
        而容器要等的锁正是调用方手里的 —— 两边互相等。

        时间戳读不出来时按"不在窗口里"处理：那是旧行为（直接停），而把读不
        出来的容器一律当启动中，会让真正的崩溃残留永远停不掉。
        """
        attrs = getattr(container, 'attrs', None)
        created = attrs.get('Created') if isinstance(attrs, dict) else None
        timestamp = self._parse_docker_timestamp(created)
        if timestamp is None:
            return False
        return time.time() - timestamp < CONTAINER_START_GRACE

    def remove_docker_containers_for_sandbox(
        self, sandbox_name: str, keep_refs=(),
    ) -> int | None:
        """Stop Worker-created containers left before namespace registration.

        A Worker crash can happen after ``docker run`` but before the container
        row is inserted. Such a container is invisible to
        ``reconcile_containers``; the stable sandbox label is the only durable
        handle available during startup recovery. ``keep_refs`` preserves
        already-registered containers for the ordered retirement path.
        Containers are **stopped, not removed** — the writable layer belongs to
        the user (see ``_stop_container``).
        ``None`` means the Docker scan or one of the stops was incomplete;
        callers must retain the sandbox record and retry rather than destroy
        the reservation.  A container that is still inside its
        create-to-registration window is one of those "incomplete" cases: it is
        skipped and the caller retries later (see ``_container_starting``).
        """
        db = getattr(self, 'db', None)
        record = db.get_sandbox(sandbox_name) if db is not None else None
        list_containers = getattr(db, 'list_containers', None)
        records = (list_containers(sandbox_name)
                   if list_containers is not None else None)
        if record is not None and not records:
            return 0
        try:
            client = docker_client(timeout=10)
        except DockerExecutorError as exc:
            logger.error(
                '恢复时无法连接 Docker，保留 sandbox %s 等待重试: %s',
                sandbox_name, exc,
            )
            return None
        keep = {str(ref) for ref in keep_refs if ref}
        stopped = 0
        failed = False
        starting = False
        found = 0
        try:
            containers = client.containers.list(
                all=True,
                filters={'label': f'neu-box.sandbox={sandbox_name}'},
            )
            for container in containers:
                found += 1
                container_id = str(getattr(container, 'id', '') or '')
                container_name = str(getattr(container, 'name', '') or '')
                if container_id in keep or container_name in keep:
                    continue
                # 没有登记、又还在启动窗口里：它可能正等我们的锁（见
                # ``_container_starting``）。跳过它，让调用方保留记录下一轮
                # 再来；窗口里的容器自己会结束 —— hook 拿到锁时看到的是
                # DESTROYING，被拒 → ``runc create`` 失败，容器起不来。
                if self._container_starting(container):
                    starting = True
                    logger.warning(
                        'sandbox %s 的容器 %s 仍在启动窗口内，延后销毁',
                        sandbox_name, container_id or container_name,
                    )
                    continue
                try:
                    # 同样只停不删（可写层留着）。已经在跑的才算一条；停下来的
                    # 容器下一轮还会被扫到，no-op 不该反复计数/刷日志。
                    if str(getattr(container, 'status', '') or '') == 'running':
                        if self._stop_container(
                                container_id or container_name) is False:
                            failed = True
                        else:
                            stopped += 1
                except Exception:
                    failed = True
                    logger.exception(
                        '恢复时停 sandbox %s 的容器失败', sandbox_name,
                    )
        except Exception:
            failed = True
            logger.exception(
                '恢复时扫描 sandbox %s 的 Docker 容器失败', sandbox_name,
            )
        finally:
            try:
                client.close()
            except Exception:
                logger.debug('关闭 Docker client 失败', exc_info=True)
        # A successful scan with no labelled containers is the only reliable
        # evidence that an interrupted startup left no Docker orphan.  Clear
        # the marker here so a pure Host sandbox does not keep depending on
        # Docker after the next reaper cycle.
        return None if (failed or starting) else stopped

    @staticmethod
    def _container_sandbox_label(container) -> Optional[str]:
        """读出容器上的 ``neu-box.sandbox`` label；读不出返回 ``None``。

        docker-py 把 label 放在 ``Container.labels``（背后就是
        ``attrs['Config']['Labels']``）。这一层要能被测试用的假对象喂进来，
        所以两条路都认。
        """
        labels = getattr(container, 'labels', None)
        if not isinstance(labels, dict):
            attrs = getattr(container, 'attrs', None)
            config = attrs.get('Config') if isinstance(attrs, dict) else None
            labels = config.get('Labels') if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            return None
        value = labels.get('neu-box.sandbox')
        if value is None:
            return None
        return str(value).strip() or None

    def reap_orphan_labelled_containers(self) -> int | None:
        """收掉 label 还在、沙盒记录已经没了的容器。

        ``remove_docker_containers_for_sandbox`` 要一个沙盒名才跑得起来。崩溃
        窗口（``docker create/start`` 之后、runtime hook 落库之前）留下的容器
        没有 ``containers`` 行，``reconcile_containers`` 看不见它；等沙盒记录
        被销毁路径删掉，按名字扫的那条路也没人再传名字进来 —— label 是它唯一
        的持久句柄。

        这一遍反过来查：不看沙盒记录，直接列 Docker 侧带 ``neu-box.sandbox``
        label 的容器，label 里的沙盒名在 DB 里查不到就收掉。它跑在销毁路径
        **之外**，于是：

        * dockerd 不可用时只返回 ``None`` 等下一轮，不阻塞任何沙盒的销毁 ——
          纯 Host 沙盒因此不需要 Docker 可用，这正是
          ``remove_docker_containers_for_sandbox`` 里那条快速返回要保住的性质；
        * 沙盒记录还在的容器一概不碰，交给它自己的销毁路径按顺序收（撤授权 →
          ``docker stop`` → 等退出 → 放 pin）。

        返回这一轮停掉的容器数（**不删**：可写层是用户的）；dockerd 不可用、
        扫描失败、或有容器这一轮停不掉时返回 ``None``。调用方只记日志、下一轮
        再来，不把它当错误。
        """
        get_sandbox = getattr(getattr(self, 'db', None), 'get_sandbox', None)
        if get_sandbox is None:
            return None
        try:
            client = docker_client(timeout=10)
        except DockerExecutorError as exc:
            # 这一遍是后台兜底，每轮都跑。纯 Host 部署根本不装 dockerd
            # （RPM 和 systemd 都没依赖它），那种情况下每 30 秒一条 error
            # 只是噪音 —— 要报错的删除失败在下面单独记。
            logger.debug('清扫无主容器时无法连接 Docker，下一轮重试: %s', exc)
            return None
        stopped = 0
        incomplete = False
        try:
            containers = client.containers.list(
                all=True, filters={'label': 'neu-box.sandbox'},
            )
            for container in containers:
                sandbox_name = self._container_sandbox_label(container)
                if not sandbox_name:
                    # filter 是按 label 存在筛的，正常取不到这一支；真取不到
                    # 就留着不删 —— 下一轮还会看见它，不该在这里猜归属。
                    incomplete = True
                    logger.warning(
                        '容器 %s 带 label 却读不出沙盒名，跳过清扫',
                        getattr(container, 'id', '') or container,
                    )
                    continue
                if get_sandbox(sandbox_name) is not None:
                    continue
                # 和销毁路径同一个理由：窗口里的容器可能正卡在 ``runc
                # create`` 的 hook 上等锁，碰它就是互等（见
                # ``_container_starting``）。它自己会因为登记被拒而起不来，
                # 下一轮就老了。
                if self._container_starting(container):
                    incomplete = True
                    logger.warning(
                        '无主容器 %s（原沙盒 %s）仍在启动窗口内，延后清扫',
                        getattr(container, 'id', '') or container, sandbox_name,
                    )
                    continue
                # 走到这里说明"此刻"DB 里确实没有这个名字。命令任务的沙盒名
                # 带 uuid、不会重名；acquire 的名字是 ``sbx_<owner>_<pid>``，
                # pid 复用后同名重建理论上留了一个极窄的误停窗口（重建要先
                # 插沙盒行，所以只有"查完 DB 到 stop 发出"这一瞬）。
                try:
                    # 只停不删：可写层是用户的，无主容器停掉就没有进程占卡了。
                    # 停下来的容器下一轮还会被扫到，no-op 不该反复计数/刷日志。
                    if str(getattr(container, 'status', '') or '') != 'running':
                        continue
                    if self._stop_container(
                            getattr(container, 'id', '') or container) is False:
                        incomplete = True
                        continue
                    stopped += 1
                    logger.warning(
                        '停掉无主容器 %s（原沙盒 %s，记录已不存在；容器保留）',
                        getattr(container, 'id', '') or container, sandbox_name,
                    )
                except Exception:
                    incomplete = True
                    logger.exception('停无主容器失败: %s', sandbox_name)
        except Exception:
            incomplete = True
            logger.exception('清扫无主容器时列出 Docker 容器失败')
        finally:
            try:
                client.close()
            except Exception:
                logger.debug('关闭 Docker client 失败', exc_info=True)
        return None if incomplete else stopped

    def _await_container_exit(self, records: list) -> None:
        """等这批容器真的退出。

        有 pidfd 的等 epoll 事件；重启之后 fd 全丢了，只能退回读 /proc
        （``identity_alive``）—— 两条路都不能少，否则重启后销毁沙盒会跳过
        "等容器停下来"这一步。

        超时说明容器杀不动（D 状态、卡在驱动的 ioctl 里）。这时必须抛出：
        一个还握着设备 fd 的进程如果被当成"已经收掉"，它的卡会带着 fd 回到
        空闲池，下一家拿到手就撞车，而且全程没有报错。
        """
        deadline = time.monotonic() + CONTAINER_EXIT_TIMEOUT
        while True:
            pending = [record for record in records
                       if self._container_alive(record)]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DockerExecutorError(
                    f'容器未在 {CONTAINER_EXIT_TIMEOUT:.0f}s 内退出，'
                    f'沙盒保持原状等待重试: '
                    f'{sorted(record["mount_namespace"] for record in pending)}',
                    'docker_container_exit_timeout',
                )
            if any(int(record['mount_namespace']) in self._container_fds
                   for record in pending):
                self._epoll.poll(timeout=remaining)
            else:
                time.sleep(min(0.2, remaining))

    def _retire_containers_of(self, sandbox_name: str) -> None:
        """沙盒销毁前停掉它名下的容器。

        ① 撤授权（容器立刻失去新的 open 权限）→ ② 异步 ``docker stop``
        → ③ 等它们真的退出 → ④ 删记录、放 pin。撤授权在最前、放 pin 在最末，
        中间不存在"账本说卡空了、实际还在用"的窗口。

        **只停不删**：删容器会连可写层一起销毁，用户可能还要 commit / cp 出来。
        停掉就够了 —— 进程一没，那个 mnt ns 就死了，驱动按它缓存的 UDA 表也就
        没人能用；容器留着，下次 ``docker start`` 重新走 hook，登记被拒就起不来。

        调用方必须**不持有** ``self.lock`` / ``_lifecycle_lock``：②③ 会真的
        等容器退出，而正在 ``runc create`` 的容器的 runtime hook 要拿这两把
        锁才能登记（见 ``_container_starting`` 与 ``destroy_sandbox``）。

        收不掉就抛 ``DockerExecutorError``：(a) 还在启动窗口里的容器 —— 我们
        不能碰它（互等），但它自己会因为登记被拒而起不来，下一轮就老了、
        可以正常收掉；(b) 扫描/删除失败、或容器 30s 不退 —— 沙盒原样保留，
        靠 DESTROYING 标记等收尸重试。两种情况都**必须**让 destroy 失败，
        不能当作收干净了往下走。
        """
        records = self.db.list_containers(sandbox_name)
        keep_refs = {
            str(value)
            for record in records
            for value in (record.get('container_ref'), record.get('container_id'))
            if value
        }
        # Also remove containers created by ``neu-sbox docker run`` that died
        # before the runtime hook could register them. Registered containers
        # are kept for the ordered revoke/kill/pidfd path below.
        if self.remove_docker_containers_for_sandbox(
                sandbox_name, keep_refs=keep_refs) is None:
            raise DockerExecutorError(
                f'无法完整扫描或清理 sandbox {sandbox_name} 的 Docker 容器'
                f'（含仍在启动窗口内的容器）',
                'docker_container_cleanup_failed',
            )
        if not records:
            return
        # ① 先撤授权（记录留着：等不到容器退出时要靠它重试，删了就没人记得
        #    这个容器还在）
        for record in records:
            self._revoke_container(record)

        # ② 再停 —— 已经打开的 fd 不受授权影响，必须真的把进程收掉；但**不删**
        #    容器，可写层留给用户（`_stop_container` 的注释说明了为什么够用）。
        threads = []
        for record in records:
            thread = threading.Thread(
                target=self._stop_container,
                args=(record['container_ref'],),
                name='docker-stop', daemon=True,
            )
            thread.start()
            threads.append(thread)
        try:
            # ③ 等它真的退出；抛出去就整条 destroy 失败，沙盒原样保留
            self._await_container_exit(records)
        finally:
            for thread in threads:
                thread.join(timeout=1)

        # ④ 确认退出之后才删记录、放 pin
        for record in records:
            mount_namespace = int(record['mount_namespace'])
            self.db.delete_container(mount_namespace)
            self._close_container_handles(mount_namespace)

    def _return_borrowed_pids(self, sandbox_name: str) -> None:
        """把 join 进来、并且记了 origin 的进程迁回它原来的 cgroup。

        只有 acquire 借出去的那个 shell 有 origin。沙盒里长出来的进程没有
        "家"可回，跟着沙盒一起收掉才是对的 —— 它们可能已经握着设备 fd，
        放出去等于让一张账上空闲的卡继续被人用。
        """
        record = self.db.get_sandbox(sandbox_name)
        if not record:
            return
        origins = record.get('origins') or {}
        if not isinstance(origins, dict) or not origins:
            return
        # 用 cgroup 的实时快照，不用 DB 里可能过期的 pids —— 漏掉一个就
        # 等于把借来的 shell 交给 destroy 杀掉。
        try:
            snapshot = self._read_cgroup_snapshot(sandbox_name)
        except OSError as exc:
            logger.error(
                "读取沙盒 '%s' 的进程快照失败，按 DB 记录迁回: %s",
                sandbox_name, exc,
            )
            snapshot = None
        if snapshot is not None:
            pids = snapshot[0]
            self.db.update_sandbox_pids(sandbox_name, pids)
        else:
            pids = record.get('pids') or []
        for pid in pids:
            origin = origins.get(str(pid))
            if not origin:
                continue
            if self.move_pid_to_cgroup(int(pid), origin):
                logger.warning(
                    "✓ PID %s 已从沙盒 '%s' 迁回 %s",
                    pid, sandbox_name, origin,
                )

    def move_pid_to_cgroup(self, pid: int, cgroup_path: str) -> bool:
        """把一个进程迁回指定的 cgroup v2 路径并核验结果。"""
        return cgroup.move_pid(pid, cgroup_path)

    def evacuate_caller(self, sandbox_name: str, pid: int) -> bool:
        """把"发起 release 的调用方"从沙盒 cgroup 里搬出去；返回是否真的搬了。

        为什么需要它：沙盒销毁的最后一步是 ``cgroup.kill``
        （``native/sandbox/src/cgroup.cpp`` 的 ``kill_processes``），cgroup 里
        剩下的进程一律 SIGKILL。而 ``neubox release`` 本身是**被借的那个 shell
        fork 出来的子进程** —— cgroup 成员身份随 fork 继承，所以它就在沙盒里，
        却没有 origin（origin 只记在被借的那一个 PID 上），于是被当成"沙盒里
        长出来的进程"一起收掉：用户看到 ``zsh: killed``、退出码 137，命令没有
        输出，脚本里 ``release && next`` 直接断。

        只搬调用方**自己**：它的子树不管，"沙盒里长出来的进程跟着沙盒一起收掉"
        这条语义也不动（那些进程可能已经握着设备 fd）。它的"家"从父进程的
        origin 推出来 —— 它本来就不属于这个沙盒，是被继承关系带进来的。

        只搬**确实住在要销毁的这个沙盒 cgroup 里**、且父进程在本沙盒记着
        origin 的 PID；任一条件不成立就原地不动（fail-closed，绝不把来路不明
        的进程放出去，也不让这个接口变成"搬任意 PID"的通道）。
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 1:
            return False
        record = self.db.get_sandbox(sandbox_name)
        if not record:
            return False
        origins = record.get('origins') or {}
        if not isinstance(origins, dict) or not origins:
            # 没有 origin 表的沙盒（命令任务的沙盒）没有"借来的进程"，
            # 也就没有可回的"家"，一律不动。
            return False

        try:
            snapshot = self._read_cgroup_snapshot(sandbox_name)
        except OSError as exc:
            logger.error(
                "释放前读取沙盒 '%s' 的进程快照失败，不搬调用方 %s: %s",
                sandbox_name, pid, exc,
            )
            return False
        if snapshot is None or pid not in snapshot[0]:
            return False

        destination = origins.get(str(pid))
        if not destination:
            parent = self._parent_pid(pid)
            destination = origins.get(str(parent)) if parent else None
        if not destination:
            logger.warning(
                "调用方 PID %s 在沙盒 '%s' 里，但它和父进程都没有 origin，"
                "不搬（会被销毁流程收掉）", pid, sandbox_name,
            )
            return False
        if not self.move_pid_to_cgroup(pid, destination):
            logger.error(
                "把调用方 PID %s 从沙盒 '%s' 搬回 %s 失败",
                pid, sandbox_name, destination,
            )
            return False
        logger.warning(
            "✓ 调用方 PID %s 已从沙盒 '%s' 搬回 %s（它自己发起的 release）",
            pid, sandbox_name, destination,
        )
        return True

    @staticmethod
    def _parent_pid(pid: int) -> int:
        """读 ``/proc/<pid>/stat`` 的 PPID；读不到返回 0。

        ``comm`` 字段自己可能带空格和括号，所以按最后一个 ``)`` 切开再取字段：
        切完之后 ``fields[0]`` 是 state，``fields[1]`` 是 ppid。
        """
        try:
            with open(f'/proc/{int(pid)}/stat', encoding='utf-8') as stream:
                fields = stream.read().rsplit(')', 1)[1].split()
        except (OSError, ValueError, IndexError):
            return 0
        try:
            return int(fields[1])
        except (IndexError, ValueError):
            return 0

    def wait_container_events(self, timeout: float) -> int:
        """阻塞等容器退出事件，顺便当收尸循环的 sleep。

        坐在收尸线程原来 ``sleep`` 的位置：超时到了照旧收尸，有容器退出就
        提前醒过来注销。同一个线程，不新增线程。
        """
        if not self._container_fds:
            time.sleep(max(0.0, timeout))
            return 0
        try:
            ready = {fd for fd, _events in self._epoll.poll(timeout=timeout)}
        except OSError as exc:
            logger.error('等待容器退出事件失败: %s', exc)
            time.sleep(max(0.0, timeout))
            return 0
        if not ready:
            return 0

        retired = 0
        for mount_namespace, handles in list(self._container_fds.items()):
            if handles[1] not in ready:
                continue
            try:
                self.release_container(mount_namespace)
                retired += 1
                logger.warning(
                    "容器已退出，归属登记已注销: mnt ns %s", mount_namespace,
                )
            except Exception:
                # 注销失败时摘掉监听，避免可读事件把循环变成忙等；记录和
                # pin 都留着，交给 reconcile_containers 重试。
                logger.exception(
                    "容器退出后注销失败，保留记录等待对账: mnt ns %s",
                    mount_namespace,
                )
                try:
                    self._epoll.unregister(handles[1])
                except OSError:
                    pass
        return retired

    def reconcile_containers(self) -> int:
        """兜底对账: 容器已经消失但 map/store 里还有条目时清掉。

        正常路径是收尸线程的 epoll 事件（容器一退出就注销）；这里兜的是
        Worker 重启（fd 全丢了）、事件处理失败留下的条目。mnt ns 的 inum
        会被内核复用，残留条目会让后来的新容器白捡一份授权，所以删除不是
        可选项。
        """
        removed = 0
        for record in self.db.list_containers():
            if self._container_alive(record):
                continue
            try:
                self.release_container(record['mount_namespace'])
                removed += 1
                logger.warning(
                    "回收容器归属: %s (mnt ns %s, 沙盒 %s)",
                    record['container_ref'], record['mount_namespace'],
                    record['sandbox_name'],
                )
            except Exception:
                logger.exception(
                    "回收容器归属失败，保留记录等待重试: mnt ns %s",
                    record['mount_namespace'],
                )
        return removed

    def retire_containers_on_startup(self) -> int:
        """启动时把**还活着的**登记容器停掉、撤绑定（容器不删）。

        崩溃重启（systemd 拉起）和正常升级（``neuboxctl pause`` → ``setup``）
        是两条路：只有升级会先把沙盒和容器排空。崩溃重启之后容器和 acquire
        沙盒都还活着，而跨崩溃续授权不闭环：

        * 容器退出监听（mnt ns fd + pidfd）是**内存态**，重启后全丢 —— 留着的
          登记没人收尸，残留的 mnt ns inum 被内核复用后会让新容器白捡一份授权
          （``reconcile_containers`` 的注释里写着这个风险）；
        * 驱动那张按 mnt ns 缓存的 UDA 表也还在（pinned map 同样跨重启存活），
          等于把一个"崩溃前发出的授权"接回来 —— 而授权必须由 Worker 现算。

        所以启动时一律失效：**停掉容器（可写层留着）**，用户 ``docker start``
        会重新走一遍 runtime hook 登记回来（真机正例：用例 82）。

        等不到容器退出就保留登记，交给收尸线程下一轮 —— 启动路径不能在这里
        无限等；已经死掉的登记由 ``reconcile_containers`` 撤掉。

        返回撤掉的登记条数。
        """
        retired = 0
        for record in self.db.list_containers():
            if self._container_alive(record):
                # Docker 不可用 / 停的动作没做成：保留登记直接下一轮，别在这里
                # 干等 ``STARTUP_CONTAINER_STOP_TIMEOUT`` 把启动拖慢。
                if self._stop_container(record['container_ref']) is False:
                    logger.warning(
                        "重启对账: 容器 %s 的 stop 没做成（dockerd 不可用？），"
                        "保留登记等下一轮",
                        record['container_ref'],
                    )
                    continue
                if not self._wait_container_stopped(
                        record, STARTUP_CONTAINER_STOP_TIMEOUT):
                    logger.warning(
                        "重启对账: 容器 %s 没在 %.0fs 内停下，保留登记等下一轮",
                        record['container_ref'], STARTUP_CONTAINER_STOP_TIMEOUT,
                    )
                    continue
            try:
                self.release_container(record['mount_namespace'])
                retired += 1
                logger.warning(
                    "重启对账: 撤掉容器归属 %s (mnt ns %s, 沙盒 %s)",
                    record['container_ref'], record['mount_namespace'],
                    record['sandbox_name'],
                )
            except Exception:
                logger.exception(
                    "重启对账: 撤容器归属失败，保留记录等待重试: mnt ns %s",
                    record['mount_namespace'],
                )
        return retired

    def _wait_container_stopped(self, record: dict, timeout: float) -> bool:
        """等这个容器的 init 真的退出；超时返回 ``False``（登记留着）。"""
        deadline = time.monotonic() + timeout
        while True:
            if not self._container_alive(record):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def _read_cgroup_snapshot(
        self,
        name: str,
    ) -> Optional[tuple[List[int], bool]]:
        """读取沙盒 cgroup 的进程快照与递归 populated 状态（见 cgroup.read_snapshot）。"""
        return cgroup.read_snapshot(name)

    def list_sandboxes(self) -> List[str]:
        """列出 DB 中所有沙盒名称。"""
        return [record['name'] for record in self.db.list_sandboxes()]

    def list_sandboxes_via_native(self, strict: bool = False) -> List[str]:
        """通过 native helper list 获取 cgroup 中实际存在的沙盒名称列表。"""
        def filesystem_names() -> List[str]:
            """Enumerate cgroups even when native BPF validation fails.

            Non-strict callers are used by maintenance/reaper.  They must
            still observe orphan cgroups when the helper refuses ``list``
            because pins are incomplete; otherwise pause could report quiet
            while cleanup has not become safe.  Strict callers retain the
            original error semantics for status/diagnostic APIs.
            """
            names: list[str] = []
            try:
                entries = os.scandir('/sys/fs/cgroup')
            except OSError:
                return names
            with entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if not entry.name.startswith('sandbox_'):
                        continue
                    candidate = entry.name[len('sandbox_'):]
                    if candidate.startswith('sbx_') and candidate.endswith('.slice'):
                        names.append(candidate)
            return sorted(set(names))

        if not hasattr(self, '_native_path'):
            if strict:
                raise RuntimeError('native sandbox helper 未配置')
            return filesystem_names()
        try:
            result = self._run_native('list')
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("native sandbox list 执行失败: %s", exc)
            if strict:
                raise RuntimeError(f'native sandbox list 执行失败: {exc}') from exc
            return filesystem_names()
        if result.returncode != 0:
            logger.error("native sandbox list 失败: %s", result.stderr.strip())
            if strict:
                raise RuntimeError(
                    f'native sandbox list 失败: {result.stderr.strip()}',
                )
            return filesystem_names()
        names = [n for n in result.stdout.strip().split('\n') if n and n != '(无)']
        return sorted(set(names) | set(filesystem_names()))
