"""沙盒资源分配管理 — 调用 native CLI 实现 cgroup v2 + eBPF 设备隔离。
沙盒状态持久化到 SQLite，防止掉线后重复开沙盒出问题。

设备分配模型:
  - 从配置读取 NEU_BOX_DEVICE_FILTER 正则匹配 /dev 下的设备名
  - 自动发现匹配的设备节点（如 nvidia0→195:0, nvidia1→195:1, ...）
  - 通过 DB 追踪每个沙盒已占用的设备
  - 调用配置的 NPU/GPU 信息脚本，排除被沙盒外进程占用的设备
  - 命令任务或 CLI acquire 按 device_num 从空闲池中分配
"""

import json
import logging
import os
import stat
import subprocess
import threading
import time
from typing import Optional, List

from neu_box.config import env_int, env_text
from neu_box.worker.executor.db import Database
from neu_box.worker.paths import sandbox_executable_path

logger = logging.getLogger(__name__)


# ==================================================================
# SbxManager — 沙盒生命周期管理（单例）
# ==================================================================

class SbxManager:
    """Worker 本地沙盒管理器（单例）。

    封装 ``neu-box-sandbox`` 的 create / join / destroy / status 调用，
    并在本地 DB 中记录每个沙盒的状态，支持重启后恢复。
    """

    _instance = None

    def __init__(self):
        self._sandbox_path = str(sandbox_executable_path())

        # 设备过滤器（正则，匹配 /dev 下设备名）
        self.device_filter = env_text("NEU_BOX_DEVICE_FILTER")

        # 本地 DB（统一 SQLite）
        self.db = Database.get_instance()

        # 线程安全
        self._lock = threading.RLock()

        # 最近一次成功查到的外部占用设备集合。None 表示尚无成功采样：
        # 此时查询失败必须把全部受管设备视为忙碌，避免启动阶段 fail-open。
        # 已有成功采样后，查询失败则沿用最近结果。
        self._last_external_busy: set[str] | None = None

        # 启动时恢复
        self._recover_on_startup()

    @classmethod
    def get_instance(cls) -> 'SbxManager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 内部工具 ─────────────────────────────────────────────────

    def _run_cli(self, *args) -> subprocess.CompletedProcess:
        """调用 native sandbox CLI，返回 CompletedProcess。"""
        cmd = [self._sandbox_path] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    @staticmethod
    def _cg_path(name: str) -> str:
        return f"/sys/fs/cgroup/sandbox_{name}"

    def _discover_device_nodes(self, root: str = '/dev') -> List[str]:
        """扫描设备目录，用 device_filter 正则匹配设备。

        .env 配置示例:
          device_filter=nvidia[0-9]+     # 只匹配 nvidia0, nvidia1, ...
          device_filter=davinci[0-9]+    # 只匹配 davinci0, davinci1, ...

        Returns:
            "major:minor" 字符串列表，如 ["195:0", "195:1", ...]
        """
        import re
        _regex = re.compile(self.device_filter) if self.device_filter else None
        if not _regex:
            return []

        devices = []
        try:
            for entry in os.listdir(root):
                path = os.path.join(root, entry)
                try:
                    if not _regex.fullmatch(entry):
                        continue
                    s = os.stat(path)
                    if not stat.S_ISCHR(s.st_mode):
                        continue
                    devices.append(f"{os.major(s.st_rdev)}:{os.minor(s.st_rdev)}")
                except OSError:
                    continue
        except OSError:
            pass
        devices.sort(key=lambda x: int(x.split(':')[1]))
        return devices

    def _get_allocated_devices(self) -> set:
        """扫描 DB 中所有活跃沙盒，汇总已分配的设备号集合。"""
        allocated = set()
        for rec in self.db.list_sandboxes():
            for dev in rec.get('devices', []):
                allocated.add(dev)
        return allocated

    def _get_external_busy_devices(self) -> set[str]:
        """调用配置的设备信息脚本，返回沙盒外已占用的设备。

        查询失败/超时/返回异常（如系统高负载下 npu-smi 卡死）时，
        若已有成功采样则沿用最近结果；若尚无成功采样，则把全部受管
        设备视为忙碌。这样启动阶段也保持 fail-closed。
        """
        managed = set(self._discover_device_nodes())
        fallback = (
            managed
            if self._last_external_busy is None
            else self._last_external_busy
        )
        path = env_text("NEU_BOX_DEVICE_INFO_SCRIPT")
        if not path:
            logger.error('未配置 NEU_BOX_DEVICE_INFO_SCRIPT')
            return set(fallback)
        try:
            output = subprocess.check_output(
                [path], timeout=10, stderr=subprocess.DEVNULL,
            )
            data = json.loads(output.decode())
            # 有受管设备时脚本必须报出 total>0；否则视为查询失败（
            # npu-smi 不可用时脚本会输出 {"total":0,...}）。
            if not managed or int(data.get('total', 0)) > 0:
                busy = {
                    device for device in managed
                    if int(device.split(':', 1)[1]) in {
                        int(value) for value in data.get('busy_ids', [])
                    }
                }
                self._last_external_busy = busy
                return busy
            logger.error('设备状态脚本返回 total=0，视为查询失败')
        except Exception as exc:
            logger.error('设备状态脚本失败: %s', exc)
        return set(fallback)

    def _list_sandbox_names(self) -> List[str]:
        """返回所有沙盒名称列表（兼容旧 SandboxDB.list_all 接口）。"""
        return [s['name'] for s in self.db.list_sandboxes()]

    def _read_cgroup_snapshot(
        self,
        name: str,
    ) -> Optional[tuple[List[int], bool]]:
        """读取 cgroup 中真实的进程快照和递归 populated 状态。

        ``cgroup.procs`` 是 PID 归属的权威来源，DB 只保存最近一次快照。
        同时读取所有子 cgroup，避免容器或用户创建子层级后漏掉进程；根
        cgroup 的 ``cgroup.events: populated`` 会递归统计整个层级，用于
        覆盖扫描过程中发生的 fork/exit 竞态。

        Returns:
            ``(pids, populated)``；cgroup 已不存在时返回 ``None``。

        Raises:
            OSError: cgroup 仍存在，但无法可靠读取其进程状态。
        """
        cgroup_path = self._cg_path(name)
        if not os.path.isdir(cgroup_path):
            return None

        def read_pids() -> List[int]:
            pids: set[int] = set()

            def raise_walk_error(exc: OSError):
                raise exc

            for root, _dirs, _files in os.walk(
                cgroup_path,
                onerror=raise_walk_error,
            ):
                procs_path = os.path.join(root, 'cgroup.procs')
                try:
                    with open(procs_path, encoding='utf-8') as stream:
                        lines = stream.read().splitlines()
                except FileNotFoundError:
                    # 子 cgroup 可以在扫描期间消失；根目录消失则由调用方
                    # 按“不存在”处理，仍存在的目录缺少控制文件属于异常。
                    if not os.path.isdir(cgroup_path):
                        return []
                    if not os.path.isdir(root):
                        continue
                    raise
                for raw in lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        pid = int(raw)
                    except ValueError as exc:
                        raise OSError(
                            f'{procs_path} 包含非法 PID: {raw!r}'
                        ) from exc
                    if pid > 0:
                        pids.add(pid)
            return sorted(pids)

        pids = read_pids()
        if not os.path.isdir(cgroup_path):
            return None

        populated = bool(pids)
        events_path = os.path.join(cgroup_path, 'cgroup.events')
        try:
            with open(events_path, encoding='utf-8') as stream:
                events = {
                    parts[0]: parts[1]
                    for line in stream
                    if len(parts := line.split()) == 2
                }
            if 'populated' in events:
                if events['populated'] not in {'0', '1'}:
                    raise OSError(
                        f'{events_path} 包含非法 populated 值: '
                        f'{events["populated"]!r}'
                    )
                populated = events['populated'] == '1'
        except FileNotFoundError:
            if not os.path.isdir(cgroup_path):
                return None
            # cgroup v2 应提供 cgroup.events；旧内核或测试替身缺失时，
            # 已递归读取的 cgroup.procs 仍可作为可靠后备。

        if populated:
            # 进程可能在第一次扫描结束后刚加入；再读一次，使 DB 快照
            # 尽量与当前 cgroup.procs 对齐。存活判断仍以 populated 为准。
            pids = read_pids()
            if not os.path.isdir(cgroup_path):
                return None
        else:
            # events 在 PID 扫描之后读取；populated=0 表示此刻整个层级
            # 已空，清除扫描早期可能读到、随后退出的 PID。
            pids = []
        return pids, populated

    def _get_free_devices(self) -> List[str]:
        """返回未分配给 sandbox 的设备节点，按 minor 排序。"""
        all_devices = set(self._discover_device_nodes())
        allocated = self._get_allocated_devices()
        external_busy = self._get_external_busy_devices()
        return sorted(
            all_devices - allocated - external_busy,
            key=lambda x: int(x.split(':')[1]),
        )

    # ── 启动恢复 ─────────────────────────────────────────────────

    def _recover_on_startup(self):
        """重启后核对 DB 与 cgroup 实际状态，清理已不存在的沙盒记录。"""
        for name in self._list_sandbox_names():
            if not os.path.isdir(self._cg_path(name)):
                logger.warning(
                    "恢复: 沙盒 '%s' 的 cgroup 已不存在，清理设备预留",
                    name,
                )
                # 仍需经过 native destroy 清理 eBPF map，不能只删 DB，否则
                # reserved_devices 中的卡会一直保持预留状态。
                if not self.destroy_sandbox(name):
                    logger.error(
                        "恢复: 沙盒 '%s' 清理失败，保留记录等待 Reaper 重试",
                        name,
                    )
            else:
                logger.info("恢复: 沙盒 '%s' 仍存活", name)

    # ── 核心操作 ─────────────────────────────────────────────────

    def create_sandbox(self, name: str, cpu: int = 0, mem: str = "0",
                       devices: Optional[List[str]] = None) -> bool:
        """创建沙盒。

        Args:
            name:    沙盒名称
            cpu:     CPU 核数 (0=不限)
            mem:     内存限制 (如 "512M", "2G", "0"=不限)
            devices: 实际 major:minor 设备号列表。None 表示不预留任何设备。

        Returns:
            True 表示创建成功（或已存在且有效）。
        """
        with self._lock:
            # 已存在且在 cgroup 中有效 → 直接返回
            if os.path.isdir(self._cg_path(name)):
                existing = self.db.get_sandbox(name)
                if existing:
                    logger.warning("沙盒 '%s' 已存在，跳过创建", name)
                    return True

            # 构建命令行
            args = ['create', name, str(cpu), mem]
            if devices:
                args.extend(devices)

            logger.warning("创建沙盒 '%s' (cpu=%s, mem=%s, devices=%s)", name, cpu, mem, devices)
            result = self._run_cli(*args)
            if result.returncode != 0:
                logger.error("创建沙盒 '%s' 失败: %s", name, result.stderr.strip())
                return False

            # 写入 DB
            self.db.insert_sandbox(
                name=name, cpu=cpu, mem=mem,
                devices=devices or [],
                cgroup_path=self._cg_path(name),
                pids=[])
            logger.warning("✓ 沙盒 '%s' 创建成功", name)
            return True

    def join_sandbox(self, name: str, pid: int) -> bool:
        """将进程加入沙盒。

        Returns:
            True 表示加入成功。
        """
        with self._lock:
            record = self.db.get_sandbox(name)
            if not record:
                logger.error("加入失败: 沙盒 '%s' 不在 DB 中", name)
                return False

            result = self._run_cli('join', name, str(pid))
            if result.returncode != 0:
                logger.error("加入 PID %s 到 '%s' 失败: %s", pid, name, result.stderr.strip())
                return False

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

            logger.warning("✓ PID %s 已加入沙盒 '%s'", pid, name)
            return True

    def move_pid_to_cgroup(self, pid: int, cgroup_path: str) -> bool:
        """将 PID 迁移到一个已存在的 cgroup v2 路径并核验结果。"""
        if not isinstance(pid, int) or pid <= 0:
            logger.error('迁移 cgroup 失败: PID 必须为正整数 (%r)', pid)
            return False

        normalized = os.path.normpath(
            '/' + str(cgroup_path or '').lstrip('/'),
        )
        cgroup_root = '/sys/fs/cgroup'
        target_dir = os.path.abspath(os.path.join(
            cgroup_root, normalized.lstrip('/'),
        ))
        try:
            if os.path.commonpath((cgroup_root, target_dir)) != cgroup_root:
                logger.error('拒绝无效 cgroup 路径: %s', cgroup_path)
                return False
        except ValueError:
            logger.error('拒绝无效 cgroup 路径: %s', cgroup_path)
            return False

        procs_path = os.path.join(target_dir, 'cgroup.procs')
        try:
            with open(procs_path, 'w', encoding='utf-8') as stream:
                stream.write(str(pid))

            actual = ''
            with open(f'/proc/{pid}/cgroup', encoding='utf-8') as stream:
                for line in stream:
                    hierarchy, _controllers, path = line.rstrip('\n').split(
                        ':', 2,
                    )
                    if hierarchy == '0':
                        actual = path.rstrip('/') or '/'
                        break
            expected = normalized.rstrip('/') or '/'
            if actual != expected:
                logger.error(
                    'PID %s cgroup 迁移核验失败: expected=%s actual=%s',
                    pid, expected, actual,
                )
                return False
        except (OSError, ValueError) as exc:
            logger.error(
                '将 PID %s 迁移到 cgroup %s 失败: %s',
                pid, normalized, exc,
            )
            return False

        logger.warning('✓ PID %s 已迁移到 cgroup %s', pid, normalized)
        return True

    def destroy_sandbox(self, name: str) -> bool:
        """销毁沙盒，清理 cgroup 和 eBPF 预留，释放设备。

        Returns:
            True 表示销毁成功（或沙盒本来就不存在）。
        """
        with self._lock:
            record = self.db.get_sandbox(name)
            if not os.path.isdir(self._cg_path(name)) and not record:
                return True

            # 即使 cgroup 目录已经消失也必须调用 CLI：CLI 会根据持久化
            # 的 cgroup owner ID 扫描并删除 eBPF map 条目。只有 CLI 成功且
            # cgroup 确认消失后才能删 DB，否则保留记录供 Reaper 重试。
            try:
                result = self._run_cli('destroy', name)
            except (OSError, subprocess.SubprocessError) as exc:
                # 单个沙盒 CLI 超时/启动失败不能打断整个 Reaper 扫描，
                # 保留 DB 和设备元数据供下一轮继续尝试。
                logger.error("销毁沙盒 '%s' 执行失败: %s", name, exc)
                return False
            if result.returncode != 0:
                logger.error("销毁沙盒 '%s' 失败: %s", name, result.stderr.strip())
                return False

            # CLI 返回成功 ≠ cgroup 目录已消失
            if os.path.isdir(self._cg_path(name)):
                logger.warning("沙盒 '%s' cgroup 目录未清除，保留 DB 记录等待重试", name)
                return False

            self.db.delete_sandbox(name)
            logger.warning("✓ 沙盒 '%s' 已销毁", name)
            return True

    def sandbox_status(self, name: str) -> Optional[dict]:
        """查询沙盒状态（调用 native CLI status）。"""
        if not os.path.isdir(self._cg_path(name)):
            return None
        result = self._run_cli('status', name)
        if result.returncode != 0:
            return None
        return {'name': name, 'output': result.stdout}

    def list_sandboxes(self) -> List[str]:
        """列出 DB 中所有沙盒名称。"""
        return self._list_sandbox_names()

    def list_sandboxes_via_cli(self) -> List[str]:
        """通过 native CLI list 获取 cgroup 中实际存在的沙盒名称列表。"""
        try:
            result = self._run_cli('list')
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("native sandbox list 执行失败: %s", exc)
            return []
        if result.returncode != 0:
            logger.error("native sandbox list 失败: %s", result.stderr.strip())
            return []
        names = [n for n in result.stdout.strip().split('\n') if n and n != '(无)']
        return names

    # ── 沙盒分配 ─────────────────────────────────────────────────

    def allocate_sandbox(self, owner: str, sandbox_id: str,
                         cpu: int = 0, mem: str = "0",
                         device_num: int = 0,
                         device_ids: Optional[List[str]] = None) -> Optional[dict]:
        """为命令任务或手动 acquire 分配沙盒。

        沙盒命名为 sbx_{owner}_{sandbox_id}.slice。

        Args:
            owner:       沙盒所有者（系统用户名）
            sandbox_id:  沙盒唯一标识（命令任务 ID 或 acquire 的 PID）
            device_num:  要分配的设备数量 (0=不分配，device_ids 为空时自动选取)
            device_ids:  用户指定的实际 major:minor 列表，优先于 device_num
            cpu/mem: 同 create_sandbox

        Returns:
            成功返回 {'sandbox_name': str, 'devices': [str]}，失败返回 None。
        """
        with self._lock:
            sandbox_name = f"sbx_{owner}_{sandbox_id}.slice"

            devices = []
            if device_ids:
                # 用户指定设备：校验是否全部空闲
                free = self._get_free_devices()
                for d in device_ids:
                    if d not in free:
                        logger.warning("指定设备 %s 不可用 (已被占用或不存在)", d)
                        return None
                devices = list(device_ids)
                logger.warning("使用指定设备: %s", devices)
            elif device_num > 0:
                # 自动分配：从空闲池选取 device_num 个
                free = self._get_free_devices()
                if len(free) < device_num:
                    logger.warning("设备不足: 需要 %s 个, DB 空闲 %s 个", device_num, len(free))
                    return None

                devices = free[:device_num]
                logger.warning("自动分配设备: %s (从空闲池 %s 选取)", devices, free)

            success = self.create_sandbox(
                sandbox_name,
                cpu=cpu,
                mem=mem,
                devices=devices if devices else None,
            )
            if not success:
                return None

            return {'sandbox_name': sandbox_name, 'devices': devices}

    # ── 孤儿清理 ─────────────────────────────────────────────────

    def cleanup_orphaned(self) -> int:
        """清理进程已退出的沙盒，释放设备资源。

        Returns:
            清理的沙盒数量。
        """
        cleaned = 0
        interval = max(
            1,
            env_int(
                "NEU_BOX_SANDBOX_REAPER_INTERVAL",
                30,
            ),
        )
        # 与 create/join/destroy/allocate 共用同一把锁，避免 Neu Box 在
        # “确认空”与 destroy 之间把新进程加入刚被判定为空的沙盒。
        with self._lock:
            sandbox_names = self._list_sandbox_names()
            logger.debug("共 %s 个沙盒待检查", len(sandbox_names))
            for name in sandbox_names:
                record = self.db.get_sandbox(name)
                if not record:
                    continue

                try:
                    snapshot = self._read_cgroup_snapshot(name)
                except OSError as exc:
                    # 无法确认时保留资源并在下一轮重试；不能把读取错误当空，
                    # 否则可能杀掉仍在运行的任务。
                    logger.error(
                        "收尸无法读取沙盒 '%s' 的 cgroup 状态，将重试: %s",
                        name, exc,
                    )
                    continue

                if snapshot is None:
                    logger.warning(
                        "清理残留沙盒记录 '%s' (cgroup 已不存在)", name
                    )
                    if self.destroy_sandbox(name):
                        cleaned += 1
                    continue

                pids, populated = snapshot
                if record.get('pids', []) != pids:
                    self.db.update_sandbox_pids(name, pids)
                if populated:
                    continue

                # create 和首次 join 之间允许一个 Reaper 周期，避免刚创建
                # 的空 cgroup 被后台线程提前回收。
                try:
                    created_at = float(record.get('created_at') or 0)
                except (TypeError, ValueError):
                    created_at = 0
                if time.time() - created_at < interval:
                    continue

                # 销毁前再次读取，覆盖第一次读取后刚发生的 join/fork/exit。
                try:
                    confirmation = self._read_cgroup_snapshot(name)
                except OSError as exc:
                    logger.error(
                        "收尸二次确认沙盒 '%s' 失败，将重试: %s",
                        name, exc,
                    )
                    continue
                if confirmation is not None:
                    confirmed_pids, confirmed_populated = confirmation
                    if confirmed_pids != pids:
                        self.db.update_sandbox_pids(name, confirmed_pids)
                    if confirmed_populated:
                        continue

                logger.warning("清理空沙盒 '%s' (cgroup 层级无进程)", name)
                if self.destroy_sandbox(name):
                    cleaned += 1

            # 补充：文件系统孤儿扫描（cgroup 目录存在但 DB 无记录）。
            # destroy_sandbox 会验证 cgroup 与 eBPF 清理结果；失败不计为
            # cleaned，下一轮仍会从文件系统再次发现并重试。
            db_names = set(self._list_sandbox_names())
            fs_names = set(self.list_sandboxes_via_cli())
            orphans = fs_names - db_names
            orphan_cleaned = []
            for name in orphans:
                logger.warning("清理文件系统孤儿沙盒 '%s' (DB 无记录)", name)
                if self.destroy_sandbox(name):
                    cleaned += 1
                    orphan_cleaned.append(name)
            if orphans:
                logger.warning(
                    "文件系统孤儿清理完成: 成功=%s, 待重试=%s",
                    sorted(orphan_cleaned),
                    sorted(orphans - set(orphan_cleaned)),
                )

        return cleaned


    # ── 定时收尸（Reaper） ───────────────────────────────────────

    def _reaper_loop(self):
        """后台收尸线程主循环。每隔 sandbox_reaper_interval 秒执行一次收尸。"""
        interval = env_int(
            "NEU_BOX_SANDBOX_REAPER_INTERVAL",
            30,
        )
        logger.warning("定时收尸已启动 (间隔=%ss)", interval)

        while True:
            try:
                t0 = time.monotonic()
                logger.debug("开始收尸扫描...")
                cleaned = self.cleanup_orphaned()
                remaining = len(self._list_sandbox_names())
                if cleaned > 0:
                    logger.warning("本轮收尸完成: 清理=%s, 剩余沙盒=%s", cleaned, remaining)
                else:
                    logger.debug("本轮收尸完成: 清理=%s, 剩余沙盒=%s", cleaned, remaining)
                # 用实际耗时修正 sleep，保证间隔稳定
                elapsed = time.monotonic() - t0
                sleep_time = max(0, interval - elapsed)
                time.sleep(sleep_time)
            except Exception as e:
                logger.error("收尸异常: %s", e, exc_info=True)
                time.sleep(interval)

    def start_reaper(self):
        """启动后台收尸线程（daemon 线程，随主进程退出）。"""
        t = threading.Thread(target=self._reaper_loop, daemon=True, name='sbx-reaper')
        t.start()
        return t
