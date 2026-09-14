"""cgroup v2 路径、进程迁移和存活快照。"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)
CGROUP_ROOT = "/sys/fs/cgroup"
CGROUP_PREFIX = "sandbox_"


def path(name: str) -> str:
    return f"{CGROUP_ROOT}/{CGROUP_PREFIX}{name}"


def list_names() -> List[str]:
    return sorted(
        entry[len(CGROUP_PREFIX):]
        for entry in os.listdir(CGROUP_ROOT)
        if entry.startswith(CGROUP_PREFIX)
        and os.path.isdir(os.path.join(CGROUP_ROOT, entry))
    )


def read_snapshot(name: str) -> Optional[tuple[List[int], bool]]:
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
    cgroup_path = path(name)
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


def move_pid(pid: int, cgroup_path: str) -> bool:
    """将 PID 迁移到一个已存在的 cgroup v2 路径并核验结果。"""
    if not isinstance(pid, int) or pid <= 0:
        logger.error('迁移 cgroup 失败: PID 必须为正整数 (%r)', pid)
        return False

    normalized = os.path.normpath(
        '/' + str(cgroup_path or '').lstrip('/'),
    )
    cgroup_root = CGROUP_ROOT
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
