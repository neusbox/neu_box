import threading

from neu_box.runtime.reaper import Reaper


def test_recovery_removes_unregistered_task_containers_before_destroy():
    sandbox = 'sbx_user_task-1.slice'
    events = []

    class DB:
        def get_queue_tasks(self):
            return [{'user_id': 'user', 'task_id': 'task-1', 'status': 'running'}]

        def list_sandboxes(self):
            return [{'name': sandbox}]

        def update_task_result(self, *args, **kwargs):
            events.append(('task_failed', args[0]))

    class Manager:
        db = DB()

        def list_sandboxes(self):
            return [sandbox]

        def remove_docker_containers_for_sandbox(self, name):
            events.append(('remove_containers', name))
            return 1

        def destroy_sandbox(self, name):
            events.append(('destroy', name))
            return True

        def reap_orphan_labelled_containers(self):
            return 0

    Reaper(Manager()).recover_on_startup()
    assert events == [
        ('remove_containers', sandbox),
        ('task_failed', 'task-1'),
        ('destroy', sandbox),
    ]


def test_recovery_keeps_records_when_orphan_container_cleanup_fails():
    sandbox = 'sbx_user_task-1.slice'
    events = []

    class DB:
        def get_queue_tasks(self):
            return [{'user_id': 'user', 'task_id': 'task-1', 'status': 'running'}]

        def list_sandboxes(self):
            return [{'name': sandbox}]

        def update_task_result(self, *args, **kwargs):
            events.append(('task_failed', args[0]))

    class Manager:
        db = DB()

        def list_sandboxes(self):
            return [sandbox]

        def remove_docker_containers_for_sandbox(self, name):
            events.append(('remove_containers', name))
            return None

        def destroy_sandbox(self, name):
            events.append(('destroy', name))
            return True

        def reap_orphan_labelled_containers(self):
            return 0

    Reaper(Manager()).recover_on_startup()
    assert events == [('remove_containers', sandbox)]


def _idle_manager(sandbox, events, containers):
    """ACTIVE 沙盒 + 假的存活容器列表；cgroup 里永远没有进程。"""

    class DB:
        def get_sandbox(self, _name):
            return {'name': sandbox, 'state': 'ACTIVE', 'created_at': 0}

        def update_sandbox_pids(self, _name, _pids):
            events.append(('update_pids', _name))

    class Manager:
        db = DB()

        def __init__(self):
            self.lock = threading.RLock()

        def list_sandboxes(self):
            return [sandbox]

        def containers_of(self, _name, alive_only=False):
            assert alive_only is True, '只按"活着的容器"判断'
            return list(containers)

        def destroy_sandbox(self, name):
            events.append(('destroy', name))
            return True

        def list_sandboxes_via_native(self):
            return []

        def reap_orphan_labelled_containers(self):
            return 0

    return Manager()


def test_reaper_does_not_collect_sandbox_with_live_containers(monkeypatch):
    """容器不在沙盒 cgroup 里，所以"空"不等于"结束"。

    有存活容器时连 cgroup 都不该去读：读了空快照就会走销毁路径，把设备预留
    和容器归属一起释放掉，容器会在失去授权的情况下继续跑。
    """
    sandbox = 'sbx_user_task-1.slice'
    events = []

    monkeypatch.setattr(
        'neu_box.runtime.reaper.cgroup.read_snapshot',
        lambda _name: (_ for _ in ()).throw(AssertionError('must not scan')),
    )
    manager = _idle_manager(
        sandbox, events,
        containers=[{'sandbox_name': sandbox, 'container_id': 'cid'}],
    )

    assert Reaper(manager).run_once() == 0
    assert events == []


def test_reaper_still_collects_sandbox_without_live_containers(monkeypatch):
    """反向保护：有容器检查不等于不再收尸。"""
    sandbox = 'sbx_user_task-1.slice'
    events = []

    monkeypatch.setattr(
        'neu_box.runtime.reaper.cgroup.read_snapshot',
        lambda _name: ([], False),
    )
    manager = _idle_manager(sandbox, events, containers=[])

    assert Reaper(manager).run_once() == 1
    assert events == [('destroy', sandbox)]


def test_reaper_retries_blocked_startup_container_cleanup():
    sandbox = 'sbx_user_task-1.slice'
    events = []

    class DB:
        destroyed = False

        def get_queue_tasks(self):
            return [{'user_id': 'user', 'task_id': 'task-1', 'status': 'running'}]

        def list_sandboxes(self):
            return [{'name': sandbox}]

        def get_sandbox(self, _name):
            return None if self.destroyed else {'name': sandbox, 'state': 'ACTIVE'}

        def update_task_result(self, *args, **kwargs):
            events.append(('task_failed', args[0]))

    class Manager:
        db = DB()

        def __init__(self):
            self.remove_attempts = 0
            self.lock = threading.RLock()

        def list_sandboxes(self):
            return [sandbox]

        def remove_docker_containers_for_sandbox(self, name):
            self.remove_attempts += 1
            events.append(('remove_containers', name))
            return None if self.remove_attempts == 1 else 1

        def destroy_sandbox(self, name):
            events.append(('destroy', name))
            self.db.destroyed = True
            return True

        def list_sandboxes_via_native(self):
            return []

        def reap_orphan_labelled_containers(self):
            return 0

    manager = Manager()
    reaper = Reaper(manager)
    reaper.recover_on_startup()
    assert events == [('remove_containers', sandbox)]
    assert reaper.run_once() == 1
    assert events == [
        ('remove_containers', sandbox),
        ('remove_containers', sandbox),
        ('task_failed', 'task-1'),
        ('destroy', sandbox),
    ]


def test_recovery_and_reaper_both_sweep_orphan_containers_by_label():
    """两条路径都要跑到"按 label 收无主容器"这一遍。

    它跑在沙盒循环**之后**：崩溃窗口留下的容器没有 ``containers`` 行，等沙盒
    记录被上面那轮清掉，按名字扫就再没有入口了 —— 这一遍是它唯一的兜底。
    """
    events = []

    class DB:
        def get_queue_tasks(self):
            return []

        def list_sandboxes(self):
            return []

    class Manager:
        db = DB()
        lock = threading.RLock()

        def list_sandboxes(self):
            return []

        def list_sandboxes_via_native(self):
            return []

        def reap_orphan_labelled_containers(self):
            events.append('sweep')
            return 0

    reaper = Reaper(Manager())

    reaper.recover_on_startup()
    assert events == ['sweep'], '启动恢复漏了无主容器清扫'

    events.clear()
    reaper.run_once()
    assert events == ['sweep'], '周期收尸漏了无主容器清扫'
