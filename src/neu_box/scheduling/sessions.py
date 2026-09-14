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
