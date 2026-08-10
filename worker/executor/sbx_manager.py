"""沙盒资源分配管理 — 调用 sandbox.sh 实现 cgroup v2 + eBPF 设备隔离。
沙盒状态持久化到 SQLite，防止掉线后重复开沙盒出问题。

设备分配模型:
  - 从 .env 读取 device_filter 正则匹配 /dev 下的设备名
  - 自动发现匹配的设备节点（如 nvidia0→195:0, nvidia1→195:1, ...）
  - 通过 DB 追踪每个沙盒已占用的设备
  - 扫描 /proc/*/fd，排除已被沙盒外进程打开的设备
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

from executor.db import Database

logger = logging.getLogger(__name__)


# ==================================================================
# SbxManager — 沙盒生命周期管理（单例）
# ==================================================================

class SbxManager:
    """Worker 本地沙盒管理器（单例）。

    封装 sandbox.sh 的 create / join / destroy / status 调用，
    并在本地 DB 中记录每个沙盒的状态，支持重启后恢复。
    """

    _instance = None

    def __init__(self):
        # 脚本路径
        default_script = os.path.join(
            os.path.dirname(__file__), '..', 'scripts', 'sanbox', 'v2', 'sandbox.sh'
        )
        self._script_path = os.getenv('sandbox_script_path', default_script)

        # 设备过滤器（正则，匹配 /dev 下设备名）
        self.device_filter = os.getenv('device_filter', '')

        # 本地 DB（统一 SQLite）
        self.db = Database.get_instance()

        # 线程安全
        self._lock = threading.RLock()

        # 启动时恢复
        self._recover_on_startup()

    @classmethod
    def get_instance(cls) -> 'SbxManager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 内部工具 ─────────────────────────────────────────────────

    def _run_script(self, *args) -> subprocess.CompletedProcess:
        """调用 sandbox.sh，返回 CompletedProcess。"""
        cmd = [self._script_path] + list(args)
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

    def get_open_device_users(self) -> dict[str, set[int]]:
        """返回通过 FD 打开受管设备的进程，格式为 {major:minor: {pid}}。

        直接检查设备 FD，不依赖厂商命令行工具，可同时覆盖 GPU/NPU。
        进程退出和 FD 关闭会与扫描并发，ENOENT 等瞬态错误按已消失处理。
        """
        managed = set(self._discover_device_nodes())
        users = {device: set() for device in managed}
        if not managed:
            return users

        try:
            proc_entries = os.scandir('/proc')
        except OSError as exc:
            logger.error('无法扫描 /proc，按全部设备忙碌处理: %s', exc)
            for pids in users.values():
                pids.add(0)
            return users

        incomplete = False
        with proc_entries:
            for entry in proc_entries:
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                fd_dir = f'/proc/{entry.name}/fd'
                try:
                    fds = os.scandir(fd_dir)
                except PermissionError:
                    incomplete = True
                    continue
                except (FileNotFoundError, OSError):
                    continue
                with fds:
                    for fd in fds:
                        try:
                            info = fd.stat(follow_symlinks=True)
                        except PermissionError:
                            incomplete = True
                            continue
                        except (FileNotFoundError, OSError):
                            continue
                        if not stat.S_ISCHR(info.st_mode):
                            continue
                        device = (
                            f'{os.major(info.st_rdev)}:'
                            f'{os.minor(info.st_rdev)}'
                        )
                        if device in users:
                            users[device].add(pid)
        if incomplete:
            logger.error('部分 /proc FD 无权读取，按全部设备忙碌处理')
            for pids in users.values():
                pids.add(0)
        return users

    def _get_open_devices(self) -> set[str]:
        return {
            device for device, pids in self.get_open_device_users().items()
            if pids
        }

    def _get_external_busy_devices(self) -> set[str]:
        """返回沙盒外已占用的设备。

        NVIDIA 的 Xorg/监控进程会长期打开所有 ``/dev/nvidiaN``，不能把
        任意 GPU FD 都视为计算占用。配置 ``dev_info_script_path`` 时使用
        GPU 状态脚本；其他节点继续使用通用 FD 扫描。脚本异常时失败关闭。
        """
        managed = set(self._discover_device_nodes())
        path = os.getenv('dev_info_script_path', '').strip()
        if not path:
            return self._get_open_devices()
        try:
            output = subprocess.check_output(
                [path], timeout=10, stderr=subprocess.DEVNULL,
            )
            data = json.loads(output.decode())
            raw_busy = data['busy_ids']
            if not isinstance(raw_busy, list):
                raise ValueError('busy_ids 不是数组')
            if int(data.get('total', -1)) < len(managed):
                raise ValueError('设备状态脚本返回的物理设备数不足')
            busy_minors = {int(value) for value in raw_busy}
        except Exception as exc:
            logger.error('设备状态脚本失败，按全部设备忙碌处理: %s', exc)
            return managed
        return {
            device for device in managed
            if int(device.split(':', 1)[1]) in busy_minors
        }

    def _list_sandbox_names(self) -> List[str]:
        """返回所有沙盒名称列表（兼容旧 SandboxDB.list_all 接口）。"""
        return [s['name'] for s in self.db.list_sandboxes()]

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
                logger.warning("恢复: 沙盒 '%s' 的 cgroup 已不存在，清理 DB 记录", name)
                self.db.delete_sandbox(name)
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
            devices: 设备号列表 (如 ["235:0", "235:1"])。None 表示不预留任何设备。

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
            result = self._run_script(*args)
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

            result = self._run_script('join', name, str(pid))
            if result.returncode != 0:
                logger.error("加入 PID %s 到 '%s' 失败: %s", pid, name, result.stderr.strip())
                return False

            # 更新 DB
            pids = record.get('pids', [])
            if pid not in pids:
                pids.append(pid)
                record['pids'] = pids
                self.db.update_sandbox_pids(name, pids)

            logger.warning("✓ PID %s 已加入沙盒 '%s'", pid, name)
            return True

    def destroy_sandbox(self, name: str) -> bool:
        """销毁沙盒，清理 cgroup 和 eBPF 预留，释放设备。

        Returns:
            True 表示销毁成功（或沙盒本来就不存在）。
        """
        with self._lock:
            if not os.path.isdir(self._cg_path(name)):
                self.db.delete_sandbox(name)
                return True

            result = self._run_script('destroy', name)
            if result.returncode != 0:
                logger.error("销毁沙盒 '%s' 失败: %s", name, result.stderr.strip())
                if not os.path.isdir(self._cg_path(name)):
                    self.db.delete_sandbox(name)
                return False

            # 脚本返回成功 ≠ cgroup 目录已消失（systemd slice 可能阻止 rmdir）
            if os.path.isdir(self._cg_path(name)):
                logger.warning("沙盒 '%s' cgroup 目录未清除，保留 DB 记录等待重试", name)
                return False

            self.db.delete_sandbox(name)
            logger.warning("✓ 沙盒 '%s' 已销毁", name)
            return True

    def sandbox_status(self, name: str) -> Optional[dict]:
        """查询沙盒状态（调用 sandbox.sh status）。"""
        if not os.path.isdir(self._cg_path(name)):
            return None
        result = self._run_script('status', name)
        if result.returncode != 0:
            return None
        return {'name': name, 'output': result.stdout}

    def list_sandboxes(self) -> List[str]:
        """列出 DB 中所有沙盒名称。"""
        return self._list_sandbox_names()

    def list_sandboxes_via_script(self) -> List[str]:
        """通过 sandbox.sh list 获取 cgroup 中实际存在的沙盒名称列表。"""
        result = self._run_script('list')
        if result.returncode != 0:
            logger.error("sandbox.sh list 失败: %s", result.stderr.strip())
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
            device_ids:  用户指定的设备号列表 (如 ["235:1","235:3"])，优先于 device_num
            cpu/mem: 同 create_sandbox

        Returns:
            成功返回 {'sandbox_name': str, 'devices': [str]}，失败返回 None。
        """
        with self._lock:
            sandbox_name = f"sbx_{owner}_{sandbox_id}.slice"
            open_users_before = (
                self.get_open_device_users()
                if device_ids or device_num > 0
                else {}
            )

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

            # create 与后续 join 之间仍可能有外部进程抢先打开设备。比较
            # 分配前后新增的 FD 使用者，避免 NVIDIA 的 Xorg 等长期句柄
            # 被误判，同时仍能发现新进程抢卡。
            if devices:
                open_users_after = self.get_open_device_users()
                raced = {
                    device for device in devices
                    if (
                        open_users_after.get(device, set())
                        - open_users_before.get(device, set())
                    )
                }
                if raced:
                    logger.warning('设备在分配期间被外部进程打开: %s', sorted(raced))
                    self.destroy_sandbox(sandbox_name)
                    return None

            return {'sandbox_name': sandbox_name, 'devices': devices}

    # ── 孤儿清理 ─────────────────────────────────────────────────

    def cleanup_orphaned(self) -> int:
        """清理进程已退出的沙盒，释放设备资源。

        Returns:
            清理的沙盒数量。
        """
        cleaned = 0
        sandbox_names = self._list_sandbox_names()
        logger.debug("共 %s 个沙盒待检查", len(sandbox_names))
        for name in sandbox_names:
            record = self.db.get_sandbox(name)
            if not record:
                continue

            pids = record.get('pids', [])
            if not pids:
                created_at = float(record.get('created_at') or 0)
                interval = max(
                    1,
                    int(os.getenv('sandbox_reaper_interval', '30')),
                )
                if time.time() - created_at < interval:
                    continue
                # 没有进程记录的沙盒：检查 cgroup.procs 是否为空
                procs_file = os.path.join(self._cg_path(name), 'cgroup.procs')
                try:
                    with open(procs_file) as f:
                        content = f.read().strip()
                    if not content:
                        logger.warning("清理空沙盒 '%s' (无进程)", name)
                        self.destroy_sandbox(name)
                        cleaned += 1
                except (OSError, IOError):
                    # cgroup 目录可能已不存在
                    self.db.delete_sandbox(name)
                    cleaned += 1
                continue

            # 检查记录的 PID 是否还活着
            all_dead = True
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    all_dead = False
                    break
                except (OSError, TypeError):
                    # TypeError: pids 字段异常（非整数），当作已死
                    pass

            if all_dead:
                # 二次确认：读 cgroup.procs，真正的权威来源
                procs_file = os.path.join(self._cg_path(name), 'cgroup.procs')
                cgroup_empty = False
                try:
                    with open(procs_file) as f:
                        cgroup_empty = not f.read().strip()
                except (OSError, IOError):
                    cgroup_empty = True  # 目录消失 = 空
                if not cgroup_empty:
                    logger.debug("沙盒 '%s' DB pid 已死但 cgroup.procs 非空，跳过", name)
                    continue

                logger.warning("清理孤儿沙盒 '%s' (所有 PID 已退出)", name)
                self.destroy_sandbox(name)
                cleaned += 1

        # 补充：文件系统孤儿扫描（cgroup 目录存在但 DB 无记录）
        db_names = set(self._list_sandbox_names())
        fs_names = set(self.list_sandboxes_via_script())
        orphans = fs_names - db_names
        for name in orphans:
            logger.warning("清理文件系统孤儿沙盒 '%s' (DB 无记录)", name)
            try:
                self._run_script('destroy', name)
            except Exception as e:
                logger.warning("孤儿沙盒 '%s' 清理失败: %s", name, e)
            cleaned += 1
        if orphans:
            logger.warning("文件系统孤儿清理完成: %s 个 (%s)", len(orphans), sorted(orphans))

        return cleaned


    # ── 定时收尸（Reaper） ───────────────────────────────────────

    def _reaper_loop(self):
        """后台收尸线程主循环。每隔 sandbox_reaper_interval 秒执行一次收尸。"""
        interval = int(os.getenv('sandbox_reaper_interval', '30'))
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
