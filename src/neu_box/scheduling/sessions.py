"""acquire 会话：请求与失败。

会话不是一次性任务：acquire 成功之后终端会一直占着沙盒，直到显式 release
或者被收尸回收。所以它的请求、失败和生命周期都是单独的形状 —— 和 submit
共用的只有"谁能拿设备"那一段。
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class AcquireRequest:
    request_id: str
    owner: str
    pid: int
    cpu: int
    mem: str
    device_num: int
    device_ids: list[str]
    device_root: str | None
    validator: Callable[[], None] | None
    created_at: float
    # 与命令任务同一套语义：0=普通，1=赶论文；越大越先拿卡。
    priority: int = 0
    result: Future = field(default_factory=Future)


class AcquireFailure(RuntimeError):
    """统一队列中的 acquire 在分配后无法完成加入。"""

    def __init__(self, message: str, code: str = "sandbox_acquire_failed"):
        self.code = code
        super().__init__(message)


def public(session: dict) -> dict:
    """会话在统一队列视图里的对外表示（``kind='acquire'``）。

    字段与任务的 ``public`` 尽量对齐（``id/status/user_id/priority/created_at/
    started_at/finished_at/device_num/devices``），额外给 ``pid``/``sandbox_name``/
    ``code``；它没有 ``command``、日志和 ``returncode`` —— 它不是命令任务，消费方
    按 ``kind`` 分支渲染。

    ``status`` 是会话状态（queued/allocating/active/released/cancelled/failed/
    interrupted），不复用任务的 completed/failed 那套词。
    """
    return {
        'kind': 'acquire',
        'id': session.get('request_id'),
        'request_id': session.get('request_id'),
        'user_id': session.get('owner') or '',
        'status': session.get('state') or '',
        'pid': session.get('pid'),
        'sandbox_name': session.get('sandbox_name'),
        'device_num': session.get('device_num', 0) or 0,
        'device_ids': session.get('device_ids') or [],
        'devices': session.get('devices') or [],
        'priority': session.get('priority', 0) or 0,
        'code': session.get('code'),
        'position': session.get('position', 0),
        'eta': session.get('eta'),
        'created_at': session.get('requested_at'),
        'started_at': session.get('acquired_at'),
        'finished_at': session.get('finished_at'),
    }
