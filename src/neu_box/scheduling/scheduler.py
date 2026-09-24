"""调度循环：唤醒、投放、让权、收割。

只有一个后台事件循环；命令执行是 asyncio task，不为每个任务开线程。队列状态
（两个有序桶、running、锁）仍然归 ``queue.py`` 的 ``TaskQueue`` —— 这里只负责
"什么时候取下一个、取哪个、取不到怎么办"，两者合成一个类。

策略是 **first-schedulable**：每轮先探测一次设备池（昂贵，所以只跑一次），再按
「优先级 DESC → FIFO」扫一遍队列，找**第一个当前资源足够**的条目投放。队首在
等卡时不会挡住后面不需要这张卡的条目。

两条硬约束（都是踩过坑换来的）：

  1. **探测必须在锁外做**。跑一次 npu-smi 要几百毫秒，持锁会把提交路径一起堵住。
  2. **每轮必须让一次权**。``_dispatch`` 里的 ``create_task`` 只是把协程放进
     就绪队列，要等当前协程让出控制权它才会跑；而这个循环又在同步地做探测和
     扫描。只让权一次也是挡不住的——必须每轮都让。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from neu_box.runtime.sandbox import SbxManager
from neu_box.scheduling import resources

logger = logging.getLogger(__name__)


class SchedulerMixin:
    """挂在 TaskQueue 上的调度循环；所有状态都还是 TaskQueue 的。"""

    def _wake_scheduler(self):
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            loop.call_soon_threadsafe(wake.set)

    def start(self):
        with self._lock:
            if self._running_flag:
                return
            self._running_flag = True
        self._scheduler_thread = threading.Thread(
            target=self._run_scheduler,
            daemon=True,
            name='task-queue-scheduler',
        )
        self._scheduler_thread.start()
        logger.info('异步任务调度器已启动')

    def _run_scheduler(self):
        asyncio.run(self._consume_loop())

    # ── 挑一个能投的 ────────────────────────────────────────────────

    def _pick_schedulable(self):
        """按调度顺序找第一个当前资源足够的条目；没有就返回 None。

       设备池每轮最多探测一次，而且只在这一轮**确实有条目要卡**时才探 —— 队列
        里全是不要卡的任务时一次 npu-smi 都不跑。
        """
        if SbxManager.get_instance().allocations_paused():
            return None
        with self._lock:
            candidates = self._ordered()
            needs_probe = any(
                any(resources.device_request(value))
                for _key, value in candidates
            )
        # 探测在 `_lock` 外（npu-smi 要几百毫秒，持它会把提交路径一起堵住）。
        # 但整个回合由 `_round_lock` 罩着，所以探测期间队列不会被改。
        free = resources.free_devices() if needs_probe else []
        with self._lock:
            for (kind, identifier), value in self._ordered():
                if resources.is_schedulable(value, free):
                    return kind, identifier
        return None

    async def _consume_loop(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while self._running_flag:
            # 一整个回合（探设备池 → 扫队列 → allocate → 摘队 + 置 running）都在
            # 回合锁里，期间没人能改队列：既不用复查"条目还在不在"，取消 pending
            # 也只要等回合结束再摘。详见 TaskQueue.__init__ 的回合锁注释。
            with self._round_lock:
                entry = self._pick_schedulable()
                if entry is not None:
                    with self._lock:
                        self._dispatching += 1
                    try:
                        self._dispatch(*entry)
                    finally:
                        with self._lock:
                            self._dispatching -= 1

            if self._active:
                # ★ 每轮让一次权。``_dispatch`` 里的 ``create_task`` 只是入队，
                #   必须让出控制权它才会跑；顺手把跑完的收掉。
                done, _pending = await asyncio.wait(
                    tuple(self._active), timeout=0.05,
                )
                for task in done:
                    try:
                        task.result()
                    except Exception:
                        logger.exception('调度队列处理失败')
            elif entry is None:
                # 没有在跑的任务：等唤醒（新提交 / 卡释放），超时兜底。
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
            else:
                # 刚投放了一个条目，但它没留下协程（acquire 是同步完成的）。
                # 让一次权就够，别在这里睡满 1 秒 —— 那会把队列吞吐压到 1/s。
                await asyncio.sleep(0)

        if self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)
