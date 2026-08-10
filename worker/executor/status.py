"""Worker 节点状态上报 — 查询本机 CPU/内存/设备资源及活跃沙盒。"""

import psutil
from flask import Blueprint

status_bp = Blueprint('status', __name__)

class Node_Manager:
    """Worker 本地节点状态管理器（单例），提供系统资源查询接口。"""

    _instance = None

    def __init__(self):
        self._cached_total_cpu: int = psutil.cpu_count(logical=True) or 0

    @classmethod
    def get_instance(cls) -> 'Node_Manager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── CPU ───────────────────────────────────────────────────

    def cpu_info(self) -> tuple[int, float]:
        """返回 (total_cores, idle_percent)。"""
        psutil.cpu_percent(interval=0.05)            # 第一次预热
        usage = psutil.cpu_percent(interval=0.05)
        idle = max(0.0, 100.0 - usage)
        return self._cached_total_cpu, idle

    # ── 内存 ──────────────────────────────────────────────────

    def mem_info(self) -> tuple[int, int]:
        """返回 (total_bytes, available_bytes)。"""
        mem = psutil.virtual_memory()
        return mem.total, mem.available

    # ── 设备信息 ───────────────────────────────────────────────

    def device_info(self) -> dict:
        """返回设备状态，以 device_filter (.env) 为准绳。

        dev_status = {minor: 1/0}，1=忙碌 0=空闲。
        故障卡从 device_filter 排除后自动不可见。
        """
        from executor.sbx_manager import SbxManager
        sbx = SbxManager.get_instance()
        all_devices = sbx._discover_device_nodes()
        all_ids = sorted(set(
            int(d.split(':')[1]) for d in all_devices
        )) if all_devices else []
        if not all_ids:
            return {'total': 0, 'idle': 0, 'dev_status': {}}

        busy = set()
        unavailable = (
            sbx._get_allocated_devices()
            | sbx._get_external_busy_devices()
        )
        for dev in unavailable:
            try:
                busy.add(int(dev.split(':')[1]))
            except (ValueError, IndexError):
                pass

        dev_status = {i: (1 if i in busy else 0) for i in all_ids}
        total = len(all_ids)
        idle = sum(1 for v in dev_status.values() if v == 0)
        return {'total': total, 'idle': idle, 'dev_status': dev_status}

    # ── 活跃沙盒 ──────────────────────────────────────────────

    def active_sandbox_count(self) -> int:
        """统计 cgroup 中实际存在的沙盒数量（通过 sandbox.sh list）。"""
        try:
            from executor.sbx_manager import SbxManager
            return len(SbxManager.get_instance().list_sandboxes_via_script())
        except Exception:
            return 0

    # ── 汇总 ──────────────────────────────────────────────────

    def collect_status(self) -> dict:
        """汇总本节点完整资源状态。"""
        total_cpu, idle_cpu = self.cpu_info()
        total_mem, idle_mem = self.mem_info()
        dev = self.device_info()
        sandboxes = self.active_sandbox_count()

        return {
            'status': 'online',
            'total_cpu': total_cpu,
            'idle_cpu': round(idle_cpu, 1),
            'total_mem': total_mem,
            'idle_mem': idle_mem,
            'total_devices': dev['total'],
            'idle_devices': dev['idle'],
            'dev_status': dev.get('dev_status', {}),
            'active_sandboxes': sandboxes,
        }


# ── 路由 ──────────────────────────────────────────────────────

@status_bp.route('/status', methods=['GET'])
def node_status():
    """返回本节点当前资源状态，供 master 查询。"""
    return Node_Manager.get_instance().collect_status(), 200
