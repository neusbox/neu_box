"""队列条目的统一形状。

队列里混着两类条目：

  * 命令任务 —— DB ``tasks`` 行的 dict（派发时会被就地改 ``status``/``_executor``）；
  * acquire 会话 —— :class:`~neu_box.scheduling.sessions.AcquireRequest` dataclass。

调度器只关心"要哪几张卡、谁的、什么时候来的、优先级多少"，所以把"两类形状怎么
读"集中在这里：加第三类条目时只改这个文件，调度循环不用再到处 `if kind ==`。

这里用函数而不是类层次，是故意的：命令任务的 dict 在派发路径上被就地修改，包一层
对象只会多一层间接；而这两类条目真正共用的也就下面这几个读取动作。
"""

from __future__ import annotations

from typing import Any

KIND_TASK = 'task'
KIND_ACQUIRE = 'acquire'
KINDS = (KIND_TASK, KIND_ACQUIRE)


def identifier(value: Any) -> str:
    """条目 id：任务的 task_id / 会话的 request_id。"""
    if isinstance(value, dict):
        return str(value.get('task_id') or '')
    return str(getattr(value, 'request_id', '') or '')


def owner(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get('user_id') or '')
    return str(getattr(value, 'owner', '') or '')


def priority(value: Any) -> int:
    """优先级：0 普通、1 赶论文（越大越先）。"""
    if isinstance(value, dict):
        raw = value.get('priority', 0)
    else:
        raw = getattr(value, 'priority', 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def created_at(value: Any) -> float:
    """条目的"提交时间"：任务的 ``created_at`` / 会话的 ``requested_at`` 都认。

    会话在 DB 里的列名是 ``requested_at``（``sessions`` 表），内存里的
    ``AcquireRequest`` 又只有 ``created_at``，统一视图里直接读 DB 行的那条路
    只有前者。少认一个的后果是 acquire 一律按 0.0 排序 —— 它会插到所有任务前
    面，``position``/``eta`` 从第一条起就是错的（真机用例 80 抓到的就是这个）。
    """
    if isinstance(value, dict):
        raw = value.get('created_at')
        if raw is None:
            raw = value.get('requested_at')
    else:
        raw = getattr(value, 'created_at', None)
        if raw is None:
            raw = getattr(value, 'requested_at', None)
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def device_request(value: Any) -> tuple[list[str], int]:
    """从条目里取出 ``(device_ids, device_num)``。

    两种形状在这里抹平 —— 调度器只关心"要哪几张卡 / 要几张"。
    """
    if isinstance(value, dict):
        ids = value.get('device_ids') or []
        num = value.get('device_num', 0) or 0
    else:
        ids = getattr(value, 'device_ids', None) or []
        num = getattr(value, 'device_num', 0) or 0
    try:
        return list(ids), int(num)
    except (TypeError, ValueError):
        return list(ids), 0


def sort_key(value: Any) -> tuple:
    """统一的排队顺序：优先级 DESC → 提交时间 ASC（同级 FIFO）。

    跨 kind 的 ``position``/``eta`` 必须用同一个键，否则 acquire 排不进 master
    看到的那个列表里。
    """
    return (-priority(value), created_at(value), identifier(value))
