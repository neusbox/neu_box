"""第 3 层 · 收尸（manifest 40、42）。

组 fixture ``reaper_ready`` 已经确认 Worker 在跑、``/maintenance`` 可读、至少有
一张空闲设备。这一组每条都要等收尸周期（默认 30s），是整套验收里最慢的一组
—— 收尸周期调小，这一组就跟着变快。

Reaper 的实际判据（``runtime/reaper.py`` 的 ``run_once``）：

  * ``CREATING`` 跳过；``DESTROYING`` 重试；
  * 名下还有活着的容器 → 跳过（容器不住在沙盒 cgroup 里，"空"不等于"已结束"）；
  * cgroup 里还有进程（递归 ``populated``）→ 保留；
  * 空 cgroup 还要满足"创建时间超过一个收尸周期"且"连续两次确认是空的"才会
    被销毁。

所以这一组没有"等 30 秒就一定没了"的假设，全部是轮询 + 明确上限。
"""

from __future__ import annotations

import secrets
import time

from deployment_support import process_alive, run_container

# 收尸周期之外再留一点余量，避免刚好压在周期边界上；轮询粒度 0.25s，
# 2s 足够跨过边界（这条用例的时长基本等于一个收尸周期，看 NEU_BOX_SANDBOX_
# REAPER_INTERVAL：默认 30s → 这条要 ~60s，验收环境调成 5s 就只要 ~12s）。
_PERSIST_MARGIN = 2.0


def test_reaper_keeps_live_child_then_reclaims(reaper_ready):
    """40 · Reaper 保留活跃子进程，并最终回收。

    父进程被借进沙盒、fork 出子进程之后被 kill —— 沙盒 cgroup 里还剩一个活
    进程。Reaper 必须把它当"在用的沙盒"留着（否则握着设备 fd 的进程被丢下、
    卡却回到了空闲池）；子进程也死了之后，才轮到回收。
    """
    baseline = reaper_ready.idle_devices()
    device = reaper_ready.idle_minors()[0]
    tag = f"reaper-{secrets.token_hex(4)}"
    parent, _ = reaper_ready.fork_child_in_place(seconds=600, tag=tag)

    # 这条用例要的是"Reaper 回收"，所以沙盒正常路径上不该由用例自己释放；
    # 但失败路径必须兜住 —— 否则 cgroup 里还活着的子进程会让它永远撤不掉。
    with reaper_ready.sandbox(
        reaper_ready.acquire_payload(parent.pid, device_ids=[device]),
    ) as sandbox:
        name = sandbox["sandbox_name"]

        child = reaper_ready.fork_now(tag=tag)
        child_cgroup = reaper_ready.process_cgroup(child)
        assert child_cgroup.endswith(f"sandbox_{name}"), (
            f"fork 出来的子进程没有落在沙盒 cgroup 里（{child_cgroup}），"
            f"这一条用例的前提不成立"
        )

        reaper_ready.kill(parent.pid)
        reaper_ready.wait_process_gone(parent.pid)
        reaper_ready.wait_process_alive(child)

        deadline = time.time() + reaper_ready.reaper_interval + _PERSIST_MARGIN
        while time.time() < deadline:
            assert reaper_ready.find_sandbox(name) is not None, (
                f"沙盒 {name} 在子进程 {child} 还活着的时候就被 Reaper 回收了"
                f"（活跃子进程被丢下，设备预留已经释放）"
            )
            assert process_alive(child), (
                f"子进程 {child} 在沙盒存续期间意外退出，用例前提被破坏"
            )
            time.sleep(reaper_ready.poll)

        # 子进程也死了 → cgroup 空了 → 下一个收尸周期回收。
        reaper_ready.kill(child)
        reaper_ready.wait_process_gone(child)
        reaper_ready.wait_sandbox_gone(name, timeout=reaper_ready.reaper_timeout)

        reaper_ready.wait_idle_at_least(baseline)
        assert device in reaper_ready.idle_minors(), (
            f"沙盒回收之后卡 {device} 没有回到空闲池: {reaper_ready.idle_minors()}"
        )


def test_release_stops_registered_container(
        reaper_ready, single_card, container_image):
    """42 · 销毁沙盒时容器被一并停掉（release 后看容器）。

    容器不住在沙盒 cgroup 里，所以 ``release`` 不能只 kill 一遍 cgroup 就完
    事 —— 它必须把名下登记过的容器真的停下，否则容器会带着已撤销的授权继续
    跑（表现成"驱动装了没生效"），而卡已经被放回空闲池。

    **只停不删**：删容器会连它的可写层一起销毁，用户可能还要 commit / cp 出
    产物；进程一没，那个 mnt ns 就死了，驱动按它缓存的 UDA 表也就没人能用。

    这一步要 dockerd + runtime，所以额外要容器组的 fixture；manifest 给这一行写
    的前置只有"1 卡"，实际还差一个容器运行时。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    terminal = single_card.spawn_terminal()
    # 用例本身要显式 release（那是被测行为），with 只是保证失败路径上不漏。
    with single_card.sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    ) as sandbox:
        name = sandbox["sandbox_name"]

        reference, result = run_container(
            single_card, container_image, annotation=name, command="sleep 600",
        )
        assert result.returncode == 0, (result.stdout or "")[:2000]
        container_id = single_card.container_id_of(reference)
        assert single_card.wait_container_registered(container_id) == name
        single_card.wait_idle_at_most(baseline - 1)

        released = single_card.client.release(
            name, timeout=single_card.task_timeout,
        )
        assert released.status == 200, released.text

        single_card.wait_container_stopped(reference)
        assert single_card.sandbox_of_container(container_id).json().get(
            "sandbox_name") is None, (
            f"沙盒释放后容器 {container_id} 仍然有登记记录"
        )
        single_card.wait_sandbox_gone(name)
        assert process_alive(terminal.pid), (
            f"release 误杀了借出去的终端 {terminal.pid}"
        )
        single_card.wait_idle_at_least(baseline)
        assert device in single_card.idle_minors(), single_card.idle_minors()
