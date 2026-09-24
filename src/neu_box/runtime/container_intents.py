"""start 借条：把"这次 start 该用哪个沙盒"从容器 annotation 里解耦出来。

``docker start`` 复用的是容器自己那份配置，annotation 里写的是建容器时的沙盒
名；而 annotation 建好就改不了 —— ``docker start`` 没有 ``--annotation``，容器
配置也只有重启 dockerd 才能改写。所以沙盒一旦 release，老容器再 start 就只剩
"起得来但零卡"一条路：可写层还在，卡拿不到。

``neubox docker start`` 因此走两段式：先拿本 shell 的沙盒存一张借条（键是
container_id + 属主），随后 hook 报上来的 register 认领它。借条**一次性**、
默认 10 秒过期；没认领就退化成原来的行为（annotation 还有效就按它绑，否则
零卡），不会因为借条存在就给谁多开一扇门。

这里只放账本本身（进程内、带锁）；HTTP 端点在 ``api/containers.py``，语义写在
``docs/worker-api.md`` 和 ``docs/container-registration.md``。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# 借条寿命。正常链是「client 存借条 → docker start → hook 登记」，1 秒内走完；
# 给 10 秒是为了容忍慢机器，又不至于让一张没人认领的借条躺到下一次无关的 start。
START_INTENT_TTL = 10.0


class StartIntentStore:
    """``(container_id, 属主) → 沙盒名`` 的短期借条簿。"""

    _instance: StartIntentStore | None = None
    _instance_lock = threading.Lock()

    def __init__(self, ttl_seconds: float = START_INTENT_TTL,
                 clock=time.monotonic):
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], dict] = {}

    @classmethod
    def get_instance(cls) -> StartIntentStore:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def lend(self, container_id: str, owner: str, sandbox_name: str) -> dict:
        """记一张借条；同一个 (容器, 属主) 重复记就是覆盖，后写的算数。"""
        now = self._clock()
        entry = {
            'container_id': container_id,
            'owner': owner,
            'sandbox_name': sandbox_name,
            'created_at': now,
            'consumed_at': None,
        }
        with self._lock:
            self._sweep_locked(now)
            self._entries[(container_id, owner)] = entry
        return entry

    def take(self, container_id: str, owner: str) -> str | None:
        """认领借条：返回沙盒名并标成已用；已经用过或过期返回 None。"""
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            entry = self._entries.get((container_id, owner))
            if entry is None or entry['consumed_at'] is not None:
                return None
            entry['consumed_at'] = now
            return entry['sandbox_name']

    def peek(self, container_id: str, owner: str) -> dict | None:
        """查一张借条（不改状态）；没有或已过期返回 None。"""
        now = self._clock()
        with self._lock:
            self._sweep_locked(now)
            entry = self._entries.get((container_id, owner))
            return dict(entry) if entry is not None else None

    def clear(self) -> None:
        """丢掉所有借条（测试用）。"""
        with self._lock:
            self._entries.clear()

    def _sweep_locked(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items()
            if now - entry['created_at'] >= self._ttl
        ]
        for key in expired:
            del self._entries[key]
