"""Worker 节点状态 HTTP API：/status，以及它要的节点快照。（/healthz 在 app.py）"""

from __future__ import annotations

import psutil
from flask import Blueprint

from neu_box import API_VERSION
from neu_box.runtime import devices
from neu_box.runtime.sandbox import SbxManager
from neu_box.scheduling import resources
from neu_box.scheduling.queue import TaskQueue

status_bp = Blueprint('status', __name__)


class NodeManager:
    """节点快照：CPU / 内存 / 设备空闲数 / 活跃沙盒数。

    只读汇总 —— 数据都来自 SbxManager 和 TaskQueue，自己不持有状态。
    """

    _instance = None

    def __init__(self):
        self._total_cpu = psutil.cpu_count(logical=True) or 0

    @classmethod
    def get_instance(cls) -> 'NodeManager':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def cpu_info(self) -> tuple[int, float]:
        psutil.cpu_percent(interval=0.05)
        usage = psutil.cpu_percent(interval=0.05)
        return self._total_cpu, max(0.0, 100.0 - usage)

    @staticmethod
    def mem_info() -> tuple[int, int]:
        memory = psutil.virtual_memory()
        return memory.total, memory.available

    @staticmethod
    def device_info() -> dict:
        sbx = SbxManager.get_instance()
        managed = devices.discover_nodes()
        minors = sorted({int(device.split(':')[1]) for device in managed})
        if not minors:
            return {'total': 0, 'idle': 0, 'dev_status': {}}
        external = devices.external_busy()
        if external is None:
            # 外部占用查不到：不确定的卡不能报空闲，全部算忙。
            return {
                'total': len(minors),
                'idle': 0,
                'dev_status': {minor: 1 for minor in minors},
            }
        busy = set()
        for device in resources.allocated_devices() | external:
            try:
                busy.add(int(device.split(':')[1]))
            except (ValueError, IndexError):
                continue
        status = {minor: (1 if minor in busy else 0) for minor in minors}
        return {
            'total': len(minors),
            'idle': sum(value == 0 for value in status.values()),
            'dev_status': status,
        }

    @staticmethod
    def active_sandbox_count() -> int:
        try:
            return len(SbxManager.get_instance().list_sandboxes_via_native())
        except Exception:
            return 0

    def collect_status(self) -> dict:
        total_cpu, idle_cpu = self.cpu_info()
        total_mem, idle_mem = self.mem_info()
        node_devices = self.device_info()
        return {
            'status': 'online',
            'total_cpu': total_cpu,
            'idle_cpu': round(idle_cpu, 1),
            'total_mem': total_mem,
            'idle_mem': idle_mem,
            'total_devices': node_devices['total'],
            'idle_devices': node_devices['idle'],
            'dev_status': node_devices['dev_status'],
            'active_sandboxes': self.active_sandbox_count(),
            'maintenance': TaskQueue.get_instance().maintenance_status(),
            'api_version': API_VERSION,
        }


@status_bp.route('/status', methods=['GET'])
def node_status():
    return NodeManager.get_instance().collect_status(), 200
