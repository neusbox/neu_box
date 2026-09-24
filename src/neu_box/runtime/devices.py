"""设备发现与外部占用采样。

"这台机器上有哪些受管卡"（扫 /dev 按 filter 匹配）和"卡上现在有谁"（跑
配置的信息脚本）—— 都是宿主机能力。给谁哪几张的决定不在这里（在
``scheduling/resources.py``）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
from typing import List

from neu_box.config import env_text

logger = logging.getLogger(__name__)


def scan_nodes(root: str = '/dev') -> List[tuple]:
    """扫描**受管设备节点**（"这台机器上有几张卡"），返回 [(路径, "major:minor"), ...]。

    这是**分配口径**: ``discover_nodes`` / 资源记账 / ``_device_major`` 都
    以它为准，所以这里只认 ``NEU_BOX_DEVICE_FILTER`` 匹配到的卡本身。容器要
    挂的辅助节点（manager / hdc / devmm_svm）**不在这里** —— 它们不是卡，
    混进来会让资源账多算出几张卡，见 :func:`node_paths`。

    .env 配置示例:
      device_filter=nvidia[0-9]+     # 只匹配 nvidia0, nvidia1, ...
      device_filter=davinci[0-9]+   # 只匹配 davinci0, davinci1, ...
    """
    pattern = env_text("NEU_BOX_DEVICE_FILTER")
    regex = re.compile(pattern) if pattern else None
    if not regex:
        return []

    nodes = []
    try:
        for entry in os.listdir(root):
            path = os.path.join(root, entry)
            try:
                if not regex.fullmatch(entry):
                    continue
                status = os.stat(path)
                if not stat.S_ISCHR(status.st_mode):
                    continue
                nodes.append((
                    path,
                    f"{os.major(status.st_rdev)}:"
                    f"{os.minor(status.st_rdev)}",
                ))
            except OSError:
                continue
    except OSError:
        pass
    nodes.sort(key=lambda item: int(item[1].split(':')[1]))
    return nodes


# 容器要挂、但**不是卡**的辅助设备节点: 昇腾驱动的"管理设备"。
#
# 清单来源是驱动对容器的默认设备集 —— Ascend Docker Runtime 和官方容器示例
# 都是把这三个和 davinciN 一起 ``--device`` / ``devices:`` 挂进去: 驱动初始化
# （device manager / DCMI）要 ``davinci_manager``，SVM 内存要 ``devmm_svm``，
# host-device 通道要 ``hisi_hdc``。缺了它们，容器里的第一次 NPU 初始化就失败
# （``execution/docker.py`` 模块注释里说的"驱动本身又必须拿到 manager / hdc"）。
#
# 它们**不能**进 ``NEU_BOX_DEVICE_FILTER``: 那个 filter 同时是分配口径，
# ``davinci_manager`` 会被记成多出来的一张卡。
AUXILIARY_DEVICE_NAMES = ('davinci_manager', 'devmm_svm', 'hisi_hdc')


def auxiliary_node_paths(root: str = '/dev') -> List[str]:
    """辅助节点的真实路径；只返回**存在且是字符设备**的那些。

    按名字逐个 ``stat``，不套 filter: 它们本来就匹配不上
    ``davinci[0-9]+``，而这正是这个函数存在的理由。节点不在（非昇腾机器 /
    驱动没装管理设备）就跳过，不影响受管卡。
    """
    paths = []
    for name in AUXILIARY_DEVICE_NAMES:
        path = os.path.join(root, name)
        try:
            status = os.stat(path)
        except OSError:
            continue
        if stat.S_ISCHR(status.st_mode):
            paths.append(path)
    return paths


def discover_nodes(root: str = '/dev') -> List[str]:
    """返回受管**卡**的 major:minor 列表，如 ["195:0", "195:1", ...]。"""
    return [device for _path, device in scan_nodes(root)]


def node_paths(root: str = '/dev') -> List[str]:
    """容器 ``--device`` 要挂的节点路径 = 受管卡 + 辅助节点。

    容器拿到全部受管节点: 强制点是 BPF 对 davinciN 的 open 判定，不是
    "节点在不在容器的 /dev 里"；而驱动本身又必须拿到 manager / hdc 才
    能初始化。给全量不削弱隔离，反而免掉了"最小挂载集"这种依赖驱动
    内部行为的假设。

    这是**挂载口径**，和 ``scan_nodes`` 的分配口径是两个集合: 后者只数卡，
    前者还要带上辅助节点。辅助节点是别的 major（实测 234 卡 / 235 manager /
    510 hdc / 511 svm），本来就不在 BPF 的受管 major 里（``devdrv_major``
    之外的设备一律放行），所以给出去不削弱 davinciN 那层强制。受管卡一张都
    没有时返回空 —— 没有受管 major 就没有 BPF，此时一个设备节点都不该给。
    """
    nodes = [path for path, _device in scan_nodes(root)]
    if not nodes:
        return []
    return nodes + auxiliary_node_paths(root)


def external_busy() -> set[str] | None:
    """跑配置的信息脚本，返回沙盒外已占用的设备；**查不到返回 None**。

    每次都重新跑脚本，不缓存 —— 外部占用是别人正在用的卡，拿旧结果当准
    会把卡分重。

    查不到 = 没配脚本 / 脚本失败 / 超时 / 返回 total=0。调用方必须当成
    "这台机器现在没有空闲卡"处理（fail-closed）
    """
    managed = set(discover_nodes())
    path = env_text("NEU_BOX_DEVICE_INFO_SCRIPT")
    if not path:
        logger.error('未配置 NEU_BOX_DEVICE_INFO_SCRIPT')
        return None
    try:
        output = subprocess.check_output(
            [path], timeout=10, stderr=subprocess.DEVNULL,
        )
        data = json.loads(output.decode())
        # 有受管设备时脚本必须报出 total>0；否则视为查询失败（
        # npu-smi 不可用时脚本会输出 {"total":0,...}）。
        if managed and int(data.get('total', 0)) <= 0:
            logger.error('设备状态脚本返回 total=0，视为查询失败')
            return None
        busy = {
            device for device in managed
            if int(device.split(':', 1)[1]) in {
                int(value) for value in data.get('busy_ids', [])
            }
        }
        return busy
    except Exception as exc:
        logger.error('设备状态脚本失败: %s', exc)
        return None
