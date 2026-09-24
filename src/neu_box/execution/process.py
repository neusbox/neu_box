"""执行后端的共用层：结果形状 + 后端接口。

host 和 docker 两个后端做的事一样 —— 起、等、停、收 —— 但一个跑在 asyncio
里、一个是阻塞的 SDK。这一层收口两件事：

  - **结果的形状**：两个后端返回同一个 dict，不再各写一份。
  - **后端的接口**：统一的 ``async run`` + ``cancel``，调度层不需要知道底下
    是谁（原来是靠 ``hasattr(executor, 'run_async')`` 猜）。
"""

from __future__ import annotations


def result(returncode: int = -1, stdout: str = '', stderr: str = '',
           timed_out: bool = False, error: str | None = None) -> dict:
    """执行结果的统一形状。"""
    return {
        'returncode': returncode,
        'stdout': stdout,
        'stderr': stderr,
        'timed_out': timed_out,
        'error': error,
    }


class CommandBackend:
    """一个命令的执行后端。

    ``run`` 是 async 的：host 后端本来就跑在事件循环里，docker 后端用
    ``asyncio.to_thread`` 把阻塞的 SDK 调用包进来 —— 对调度层来说两者一样。
    """

    async def run(self, timeout: int | None) -> dict:
        """跑完并返回 :func:`result` 那个形状。"""
        raise NotImplementedError

    def cancel(self) -> None:
        """让这个任务停下来并收干净（幂等）。"""
        raise NotImplementedError
