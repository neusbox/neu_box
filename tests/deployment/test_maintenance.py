"""第 3 层 · 停机维护（manifest 17、18、36、41、43、54）。

这一组全是**会动服务**的用例：`POST /maintenance/pause`、SIGTERM MainPID、
`neuboxctl setup` 起服、停服窗口里的容器行为、以及重启时的对账。它们必须排在
整套验收的最后 —— 停服会把并发用例的沙盒和任务一起带走。

为什么单独成组：这些用例彼此之间是"先后顺序有意义"的，散在基本盘/容器/调度/
收尸四个文件里时，只能靠 ``deployment_restart`` 标记在收集阶段把它们拽到最后，
看输出的人完全读不出结构（旧版就是那样）。现在它们都在这个文件里，组顺序本身
就是执行顺序（见 ``conftest._FILE_ORDER``）。

停/起服的机制（全组共用）：单元是 ``RefuseManualStop=yes``，``systemctl
stop/restart`` 会被拒；所以停服是给 MainPID 发 SIGTERM（和 ``neuboxctl
pause`` 停服做的事一样），起服是 ``neuboxctl setup``（它会迁移配置与数据库、
清掉遗留的 ``.paused`` 标记、恢复调度）。
"""

from __future__ import annotations

import secrets

import pytest

from deployment_support import (
    process_alive,
    require_container_not_running,
    run_container,
)


@pytest.mark.deployment_restart
def test_pause_refuses_new_work_until_restart(basic):
    """17 · pause 拒新请求 → resume。

    ``POST /maintenance/pause`` 是一次**停机维护**的开始：Worker 停止接受新任务
    和新沙盒，并且此后 ``resume`` 会被拒（409 ``maintenance_in_progress``）——
    恢复调度的唯一路径是让进程重启。所以这条用例以重启收尾，否则后面的用例全会
    撞在暂停态上。

    重启由 ``restart_worker()`` 完成：先给 MainPID 发 SIGTERM（``systemctl
    stop/restart`` 被单元的 ``RefuseManualStop=yes`` 拒绝），再用
    ``neuboxctl setup`` 拉起 —— 后者会清掉遗留的 ``.paused`` 标记并恢复
    调度，所以重启之后 Worker 一定能接任务。
    """
    health = basic.client.maintenance()
    assert health.status == 200, health.text
    assert health.value("maintenance").get("paused") is False, (
        "用例开始前 Worker 就已经是暂停态，无法验证 pause 的效果"
    )

    paused = basic.client.pause()
    assert paused.status == 200, paused.text
    state = paused.value("maintenance")
    assert state.get("paused") is True, state
    assert state.get("pause_in_progress") is True, state

    task = basic.client.create_task(basic.task_payload("true", device_num=0))
    assert task.status == 503, (
        f"暂停期间新任务没有被拒绝（HTTP {task.status}）:\n{task.text[:500]}"
    )
    assert task.value("code") == "worker_paused", task.text

    terminal = basic.spawn_terminal()
    acquire = basic.client.acquire(
        basic.acquire_payload(terminal.pid, device_num=0),
    )
    assert acquire.status == 503, (
        f"暂停期间 acquire 没有被拒绝（HTTP {acquire.status}）:\n{acquire.text[:500]}"
    )
    assert acquire.value("code") == "worker_paused", acquire.text

    resumed = basic.client.resume()
    assert resumed.status == 409, (
        f"维护进行中 resume 返回 HTTP {resumed.status}，应为 409；"
        f"暂停维护期间恢复调度会让 pause 等的那一堆状态重新动起来:\n"
        f"{resumed.text[:500]}"
    )
    assert resumed.value("code") == "maintenance_in_progress", resumed.text

    still = basic.client.maintenance().value("maintenance")
    assert still.get("paused") is True, (
        f"被拒绝的 resume 之后 Worker 竟然恢复调度了: {still}"
    )

    basic.restart_worker()
    restored = basic.client.create_task(basic.task_payload("true", device_num=0))
    assert restored.status == 202, (
        f"重启后 Worker 仍然不接受任务（HTTP {restored.status}）:\n"
        f"{restored.text[:500]}"
    )
    basic.wait_task(restored.value("task_id"))


@pytest.mark.deployment_restart
def test_restart_recovers_state(basic):
    """18 · 服务重启后状态恢复。

    重启的机制是 SIGTERM MainPID + ``neuboxctl setup``（见
    ``restart_worker()``）：``systemctl restart`` 被单元拒绝，也不用
    ``neuboxctl pause`` —— 那个会先等运行中的任务结束，而这条用例
    恰恰要在任务还在跑的时候把 Worker 停掉，看启动恢复怎么收尾它。
    """
    baseline_idle = basic.idle_devices()
    baseline_total = basic.total_devices()

    task_id = basic.submit("sleep 20", device_num=0)
    running = basic.wait_task_running(task_id)
    assert running["status"] == "running", running

    basic.restart_worker()

    task = basic.wait_task(task_id)
    assert task["status"] == "failed", (
        f"重启前仍在 running 的任务重启后应为 failed，实际为 {task['status']}"
    )
    assert "重启" in (task["result"].get("error") or ""), (
        f"孤儿任务的失败原因没有说明是 Worker 重启: {task['result']}"
    )

    status = basic.status()
    assert status["total_devices"] == baseline_total, (
        f"重启后 total_devices 从 {baseline_total} 变成 {status['total_devices']}"
    )
    assert status["maintenance"]["paused"] is False, status["maintenance"]
    basic.wait_idle_at_least(baseline_idle)

    assert basic.sandboxes() is not None  # /sandbox/list 必须可用
    for record in basic.sandboxes():
        assert record.get("state") in {"ACTIVE", "CREATING", "DESTROYING"}, (
            f"重启后沙盒 {record.get('name')} 的状态异常: {record.get('state')!r}"
        )

    fresh = basic.submit("printf 'after-restart\\n'", device_num=0)
    assert basic.wait_task(fresh)["status"] == "completed", "重启后新任务跑不通"

@pytest.mark.deployment_restart
def test_worker_down_blocks_annotated_container(container, container_image):
    """36 · Worker 停掉 → 容器起不来。

    停服用 ``stop_worker()``（SIGTERM MainPID，和 ``pause`` 停服做的事一样；
    ``systemctl stop`` 被单元的 ``RefuseManualStop=yes`` 拒绝），恢复用
    ``start_worker()``（``neuboxctl setup``）。这里不能用
    ``neuboxctl pause`` 停：它要先等到 ``quiet=true``，而本用例手里
    还攥着一个 active 沙盒，pause 只会一直等。
    """
    container.require_service_control()
    terminal = container.spawn_terminal()
    with container.sandbox(
        container.acquire_payload(terminal.pid, device_num=0),
    ) as sandbox:
        name = sandbox["sandbox_name"]

        container.stop_worker()
        try:
            reference, down = run_container(
                container, container_image, annotation=name, command="sleep 60",
                detach=False,
            )
            assert down.returncode != 0, (
                f"Worker 已经停了，带 annotation 的容器居然还能起来 —— hook 连不上 "
                f"Worker 时必须退非 0:\n{(down.stdout or '')[:2000]}"
            )
            require_container_not_running(
                container, reference,
                context="Worker 停着的时候 hook 连不上 Worker",
            )
            container.remove_container(reference)
        finally:
            container.start_worker()

        reference, up = run_container(
            container, container_image, annotation=name, command="sleep 60",
        )
        assert up.returncode == 0, (
            f"Worker 恢复后同一个容器仍然起不来（退出码 {up.returncode}）:\n"
            f"{(up.stdout or '')[:2000]}"
        )
        container_id = container.container_id_of(reference)
        assert container.wait_container_registered(container_id) == name

        container.remove_container(reference)

@pytest.mark.deployment_restart
def test_restart_reconciles_orphan_registration(single_card):
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
    single_card.require_service_control()
    # 前置必须先验：造不出非宿主 mount namespace 时立刻失败，而不是先占住一
    # 张卡、留下 ``sleep 600`` 的终端，再把失败现场交给后面的用例。
    # ``unshare --mount`` 在部分环境里命令在、进程活着，但 ns 根本没换。
    orphan = single_card.spawn_foreign_namespace_process(seconds=600)

    try:
        baseline = single_card.idle_devices()
        device = single_card.idle_minors()[0]
        terminal = single_card.spawn_terminal()
        # 沙盒用 with：这一块里任何断言失败都不会把卡留在场上。
        with single_card.sandbox(
            single_card.acquire_payload(terminal.pid, device_ids=[device]),
        ) as sandbox:
            name = sandbox["sandbox_name"]
            container_id = secrets.token_hex(32)

            registered = single_card.client.register_container({
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
            assert single_card.sandbox_of_container(container_id).json().get(
                "sandbox_name") == name

            single_card.stop_worker()
            try:
                single_card.kill(orphan.pid)
                single_card.wait_process_gone(orphan.pid)
            finally:
                single_card.start_worker()

            single_card.wait_container_unregistered(container_id)

            # 对账只清容器：沙盒自己是好的，借出去的终端也不该被牵连。
            record = single_card.find_sandbox(name)
            assert record is not None, (
                f"重启对账把沙盒 {name} 也一起清掉了；启动恢复只该丢弃孤儿的"
                f"容器登记"
            )
            assert record["state"] == "ACTIVE", record
            assert process_alive(terminal.pid), (
                f"重启之后借出去的终端 {terminal.pid} 不见了"
            )
    finally:
        # 沙盒替身进程不能比用例活得久：它和别的 ``sleep 600`` 不一样，
        # 外面没有任何东西会来收它。
        if process_alive(orphan.pid):
            single_card.kill(orphan.pid)
    single_card.wait_idle_at_least(baseline)

@pytest.mark.deployment_restart
def test_neuboxctl_pause_setup_roundtrip(basic):
    """43 · 公开 `neuboxctl pause` / `neuboxctl setup` 的升级路径。"""
    before = basic.status()
    assert before["maintenance"]["paused"] is False, before["maintenance"]

    # "等运行中的任务结束"是 pause 的**产品行为**，要保留；但用例没必要陪它无限
    # 等：给它自己 60s 的等待预算，超了由 neuboxctl 报错退出（报错信息里带着
    # 当前 maintenance 状态，比被外面 SIGKILL 好查）。外层 90s 只是兜底 ——
    # 60s 等待之外还有备份库、sandbox cleanup、停服三段要做。
    paused = basic.run_ctl("pause", "--timeout", "60", timeout=90)
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

@pytest.mark.deployment_restart
def test_pause_keeps_queued_task_until_setup(basic):
    """54 · pause 期间已排队的任务保留，setup 之后继续跑。

    ``POST /maintenance/pause`` 的契约是"停止接收新任务、已经排队的 pending
    保留、不自动中断"。所以占卡的任务跑完、Worker 进入暂停之后，队列里那条仍
    然在；恢复调度的唯一路径是重启（``setup``），起来之后它才继续执行。
    """
    baseline = basic.idle_devices()
    device = basic.idle_minors()[0]
    marker = f"PAUSED_QUEUED_{secrets.token_hex(4)}"

    blocker = basic.submit("sleep 8", device_ids=[device])
    basic.wait_task_running(blocker)
    queued = basic.submit(
        f"printf '%s\\n' {marker!r}", device_ids=[device],
    )

    paused = basic.client.pause()
    assert paused.status == 200, paused.text
    assert paused.value("maintenance").get("paused") is True, paused.text

    # 占卡的跑完了（pause 不打断它），但暂停期间调度不会动队列里那条。
    basic.wait_task(blocker)
    entry = basic.queue_entry(queued)
    assert entry is not None and entry["status"] == "queued", (
        f"暂停期间排队任务 {queued} 被丢了或者被执行了: {entry}；"
        f"pause 的契约是 pending 保留、不自动中断"
    )
    assert basic.client.task(queued).status == 200, "暂停期间任务查询不该失败"

    basic.restart_worker()
    task = basic.wait_task(queued)
    assert task["status"] == "completed", task.get("result")
    assert marker in basic.task_log_text(queued), (
        f"恢复之后这条排队任务没有真的执行（日志里没有 {marker}）:\n"
        f"{basic.task_log_text(queued)[:1000]}"
    )
    basic.wait_idle_at_least(baseline)
