"""第 3 层 · 慢（manifest 40-42）。

组夹具 ``slow`` 已经确认 Worker 在跑、Reaper 线程在周期扫描、至少有一张空闲
设备。这一组每条都要等收尸周期（默认 30s），所以单列一组。

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
import shutil
import time

import pytest

from deployment_support import process_alive, run_container

# 收尸周期之外再留一点余量，避免刚好压在周期边界上。
_PERSIST_MARGIN = 15.0


def test_reaper_keeps_live_child_then_reclaims(slow):
    """40 · Reaper 保留活跃子进程，并最终回收。

    父进程被借进沙盒、fork 出子进程之后被 kill —— 沙盒 cgroup 里还剩一个活
    进程。Reaper 必须把它当"在用的沙盒"留着（否则握着设备 fd 的进程被丢下、
    卡却回到了空闲池）；子进程也死了之后，才轮到回收。
    """
    baseline = slow.idle_devices()
    device = slow.idle_minors()[0]
    tag = f"reaper-{secrets.token_hex(4)}"
    parent, _ = slow.fork_child_in_place(seconds=600, tag=tag)

    sandbox = slow.acquire_sandbox(
        slow.acquire_payload(parent.pid, device_ids=[device]),
    )
    name = sandbox["sandbox_name"]

    child = slow.fork_now(tag=tag)
    child_cgroup = slow.process_cgroup(child)
    assert child_cgroup.endswith(f"sandbox_{name}"), (
        f"fork 出来的子进程没有落在沙盒 cgroup 里（{child_cgroup}），"
        f"这一条用例的前提不成立"
    )

    slow.kill(parent.pid)
    slow.wait_process_gone(parent.pid)
    slow.wait_process_alive(child)

    deadline = time.time() + slow.reaper_interval + _PERSIST_MARGIN
    while time.time() < deadline:
        assert slow.find_sandbox(name) is not None, (
            f"沙盒 {name} 在子进程 {child} 还活着的时候就被 Reaper 回收了"
            f"（活跃子进程被丢下，设备预留已经释放）"
        )
        assert process_alive(child), (
            f"子进程 {child} 在沙盒存续期间意外退出，用例前提被破坏"
        )
        time.sleep(slow.poll)

    # 子进程也死了 → cgroup 空了 → 下一个收尸周期回收。
    slow.kill(child)
    slow.wait_process_gone(child)
    slow.wait_sandbox_gone(name, timeout=slow.reaper_timeout)

    slow.wait_idle_at_least(baseline)
    assert device in slow.idle_minors(), (
        f"沙盒回收之后卡 {device} 没有回到空闲池: {slow.idle_minors()}"
    )


@pytest.mark.deployment_restart
def test_restart_reconciles_orphan_registration(slow):
    """41 · Worker 重启后对账掉孤儿容器登记。

    造一条"容器已经消失、登记还在"的记录：把一个位于别的 mount namespace 的
    进程登记成容器，然后在 Worker 停着的时候把它杀掉 —— 收尸线程的 pidfd
    epoll 收不到这个事件（进程都重启了，fd 全丢），只剩启动时的
    ``reconcile_containers`` 兜底。

    没人兜底的话，残留的 mnt ns inum 在内核复用之后会让新容器白捡一份授权。

    停/起用 ``stop_worker()`` / ``start_worker()``（SIGTERM MainPID 后由
    ``neuboxctl setup`` 拉起）：单元的 ``RefuseManualStop=yes`` 拒绝
    ``systemctl stop/restart``，而 ``neuboxctl pause`` 会先等沙盒
    全部回收 —— 本用例正需要带着一个 active 沙盒重启。启动对账
    （``SbxManager.reconcile_containers``）在标记检查之前执行，所以走 setup
    的暂停启动同样会跑到。
    """
    slow.require_service_control()
    if shutil.which("unshare") is None:
        pytest.fail(
            "前置缺失：找不到 unshare 命令，造不出一个非宿主 mount namespace "
            "的容器替身",
            pytrace=False,
        )

    baseline = slow.idle_devices()
    device = slow.idle_minors()[0]
    terminal = slow.spawn_terminal()
    sandbox = slow.acquire_sandbox(
        slow.acquire_payload(terminal.pid, device_ids=[device]),
    )
    name = sandbox["sandbox_name"]

    # ``unshare --mount`` 之后 exec 的还是同一个 PID，只是 mnt ns 换了 ——
    # 登记接口认的正是"与宿主不同的 mount namespace"。
    orphan = slow.spawn(["unshare", "--mount", "sleep", "600"])
    slow.wait_process_alive(orphan.pid)
    container_id = secrets.token_hex(32)

    registered = slow.client.register_container({
        "container_id": container_id,
        "host_pid": orphan.pid,
        "sandbox_cgroup": name,
    })
    assert registered.status == 201, (
        f"登记容器替身失败（HTTP {registered.status}）:\n"
        f"{registered.text[:1000]}\n"
        f"409 same_mount_namespace 说明 unshare 没有真的开出新的 mount "
        f"namespace；409 registered_elsewhere 说明这个 mnt ns 已经被别的"
        f"沙盒登记了"
    )
    assert slow.sandbox_of_container(container_id).json().get("sandbox_name") == name

    slow.stop_worker()
    try:
        slow.kill(orphan.pid)
        slow.wait_process_gone(orphan.pid)
    finally:
        slow.start_worker()

    slow.wait_container_unregistered(container_id)

    # 对账只清容器：沙盒自己是好的，借出去的终端也不该被牵连。
    record = slow.find_sandbox(name)
    assert record is not None, (
        f"重启对账把沙盒 {name} 也一起清掉了；启动恢复只该丢弃孤儿的容器登记"
    )
    assert record["state"] == "ACTIVE", record
    assert process_alive(terminal.pid), f"重启之后借出去的终端 {terminal.pid} 不见了"

    slow.release_sandbox(name)
    slow.wait_idle_at_least(baseline)


def test_release_reclaims_registered_container(slow, single_card, container_image):
    """42 · 销毁沙盒时容器被一并回收（release 后看容器）。

    容器不住在沙盒 cgroup 里，所以 ``release`` 不能只 kill 一遍 cgroup 就完
    事 —— 它必须把名下登记过的容器真的删掉，否则容器会带着已撤销的授权继续
    跑（表现成"驱动装了没生效"），而卡已经被放回空闲池。

    这一步要 dockerd + runtime，所以额外要容器组的夹具；manifest 给这一行写
    的前置只有"1 卡"，实际还差一个容器运行时。
    """
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]
    terminal = single_card.spawn_terminal()
    sandbox = single_card.acquire_sandbox(
        single_card.acquire_payload(terminal.pid, device_ids=[device]),
    )
    name = sandbox["sandbox_name"]

    reference, result = run_container(
        single_card, container_image, annotation=name, command="sleep 600",
    )
    assert result.returncode == 0, (result.stdout or "")[:2000]
    container_id = single_card.container_id_of(reference)
    assert single_card.wait_container_registered(container_id) == name
    single_card.wait_idle_at_most(baseline - 1)

    released = single_card.client.release(name, timeout=single_card.task_timeout)
    assert released.status == 200, released.text

    single_card.wait_container_removed(reference)
    assert single_card.sandbox_of_container(container_id).json().get(
        "sandbox_name") is None, (
        f"沙盒释放后容器 {container_id} 仍然有登记记录"
    )
    single_card.wait_sandbox_gone(name)
    assert process_alive(terminal.pid), f"release 误杀了借出去的终端 {terminal.pid}"
    single_card.wait_idle_at_least(baseline)
    assert device in single_card.idle_minors(), single_card.idle_minors()


@pytest.mark.deployment_restart
def test_neuboxctl_pause_setup_roundtrip(basic):
    """43 · 公开 `neuboxctl pause` / `neuboxctl setup` 的升级路径。"""
    before = basic.status()
    assert before["maintenance"]["paused"] is False, before["maintenance"]

    paused = basic.run_ctl("pause", "--timeout", "0", timeout=300)
    assert "数据库备份:" in paused.stdout, paused.stdout
    assert "配置备份:" in paused.stdout, paused.stdout
    assert not basic.service_active(), "neuboxctl pause 返回后服务仍在运行"
    basic.wait_worker_down(timeout=20.0)

    basic.start_worker(timeout=90)
    health = basic.client.healthz()
    assert health.status == 200, health.text
    assert health.json()["status"] == "ok", health.text

    after = basic.status()
    assert after["maintenance"]["paused"] is False, after["maintenance"]
    assert after["active_sandboxes"] == 0, after
