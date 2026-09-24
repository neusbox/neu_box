"""可用资源计算与分配决策。

"这次给谁哪几张卡"在这里定：空闲池 = 全部受管设备 − 沙盒已占 − 沙盒外占用；
指定了就逐个查在不在池子里，没指定就取前 N 张，池子不够就返回 ``None``（由
调度层退避重试）。落地（建 cgroup、写 BPF map）交给 ``runtime``。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from neu_box.scheduling import entries
from neu_box.runtime import devices
from neu_box.runtime.sandbox import SbxManager
from neu_box.storage import Database

logger = logging.getLogger(__name__)


def allocated_devices() -> set:
    """DB 里所有沙盒占用的设备号集合。"""
    allocated = set()
    for record in Database.get_instance().list_sandboxes():
        for device in record.get('devices', []):
            allocated.add(device)
    return allocated


def free_devices(allocated: Optional[set] = None) -> List[str]:
    """空闲池，按 minor 排序；查不到外部占用时返回空（没有可分配的卡）。

    ``allocated`` 只在调用方已经查过时传入。
    """
    external = devices.external_busy()
    if external is None:
        # fail-closed：不知道外面谁在用，就当一张空闲的都没有，让条目退避
        # 重试。宁可多等一轮，也不能把别人正在用的卡分出去。
        return []
    managed = set(devices.discover_nodes())
    busy = allocated_devices() if allocated is None else allocated
    return sorted(
        managed - busy - external,
        key=lambda item: int(item.split(':')[1]),
    )


def device_request(value) -> tuple[List[str], int]:
    """从条目里取出 ``(device_ids, device_num)``。

    形状的抹平在 :mod:`neu_box.scheduling.entries`（命令任务是 dict、acquire 是
    dataclass，加第三类条目只改那里）；这里保留同名函数给既有的调用方。
    """
    return entries.device_request(value)


def is_schedulable(value, free: List[str]) -> bool:
    """预判这个条目在当前空闲池下拿不拿得到设备（**不占任何资源**）。

    和 :func:`allocate` 的门控保持一致：指定了 ``device_ids`` 就要求全部空闲；
    只给 ``device_num`` 就要求空闲数够；都不申请则恒可调度（cpu/mem 只是 cgroup
    限额，不参与门控）。预判通过之后 allocate 仍可能失败（探测到分配之间的窗口），
    那时这一轮跳过它、下一轮重扫。
    """
    ids, num = device_request(value)
    if ids:
        available = set(free)
        return all(device in available for device in ids)
    return num <= 0 or len(free) >= num


def allocate(owner: str, sandbox_id: str, cpu: int = 0, mem: str = "0",
             device_num: int = 0,
             device_ids: Optional[List[str]] = None) -> Optional[dict]:
    """选设备并交给 runtime 建沙盒；拿不到资源返回 ``None``。

    沙盒命名为 ``sbx_{owner}_{sandbox_id}.slice``。
    """
    sbx = SbxManager.get_instance()
    with sbx.allocation_guard():
        sandbox_name = f"sbx_{owner}_{sandbox_id}.slice"

        devices_for_this = []
        if device_ids:
            # 用户指定设备：校验是否全部空闲
            free = free_devices()
            for device in device_ids:
                if device not in free:
                    logger.warning(
                        "指定设备 %s 不可用 (已被占用或不存在)", device)
                    return None
            devices_for_this = list(device_ids)
            logger.warning("使用指定设备: %s", devices_for_this)
        elif device_num > 0:
            # 自动分配：从空闲池选取 device_num 个
            free = free_devices()
            if len(free) < device_num:
                logger.warning(
                    "设备不足: 需要 %s 个, 空闲 %s 个", device_num, len(free))
                return None
            devices_for_this = free[:device_num]
            logger.warning(
                "自动分配设备: %s (从空闲池 %s 选取)", devices_for_this, free)

        if not sbx.create_sandbox(
            sandbox_name,
            cpu=cpu,
            mem=mem,
            devices=devices_for_this if devices_for_this else None,
        ):
            return None

        return {'sandbox_name': sandbox_name, 'devices': devices_for_this}
