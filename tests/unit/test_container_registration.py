"""容器归属登记：登记事务本身，以及 OCI runtime hook 端点。

端点行为按 ``docs/container-registration.md`` 校验：状态码、错误码、幂等，
以及「读沙盒状态」和「写登记」在同一个 ``lifecycle_lock`` 临界区里。
"""

import os
import threading
import time

import pytest
from flask import Flask

from neu_box.api.containers import container_bp
from neu_box.runtime import containers
from neu_box.runtime.containers import ContainerIdentity, DockerExecutorError
from neu_box.runtime.sandbox import SbxManager

SANDBOX = 'sbx_user_task-1.slice'


def _identity(namespace=42, container_id='cid', host_pid=123, cgroup='/docker/cid'):
    return ContainerIdentity(
        container_ref=container_id,
        container_id=container_id,
        init_host_pid=host_pid,
        init_start_time=1,
        mount_namespace=namespace,
        container_cgroup=cgroup,
        started_at='',
    )


class _DB:
    """最小 DB 替身：只实现登记路径读的几个方法。"""

    def __init__(self, sandboxes=None, containers=None):
        self.sandboxes = {
            name: dict(record) for name, record in (sandboxes or {}).items()
        }
        self.containers = dict(containers or {})
        self.activations = []

    def get_sandbox(self, name):
        return self.sandboxes.get(name)

    def get_container(self, mount_namespace):
        return self.containers.get(int(mount_namespace))

    def activate_sandbox(self, name, pids):
        """对齐 ``storage.activate_sandbox``：只有 CREATING 那一行能改。"""
        self.activations.append((name, list(pids)))
        record = self.sandboxes.get(name)
        if record is None or record.get('state') != 'CREATING':
            return False
        record['state'] = 'ACTIVE'
        record['pids'] = list(pids)
        return True


def _manager(sandboxes=None, containers=None):
    """真 ``register_runtime_container`` + 假 DB，只把 native 写入换掉。"""
    manager = SbxManager.__new__(SbxManager)
    manager.db = _DB(sandboxes=sandboxes, containers=containers)
    manager.lock = threading.RLock()
    manager._container_registration_lock = threading.RLock()

    def register_container(sandbox_name, identity):
        record = {
            'sandbox_name': sandbox_name,
            'container_ref': identity.container_ref,
            'container_id': identity.container_id,
            'init_host_pid': identity.init_host_pid,
            'init_start_time': identity.init_start_time,
            'mount_namespace': identity.mount_namespace,
        }
        manager.db.containers[int(identity.mount_namespace)] = record
        return record

    manager.register_container = register_container
    return manager


def _active_sandbox(name=SANDBOX):
    return {name: {'name': name, 'state': 'ACTIVE'}}


def _creating_sandbox(name=SANDBOX, pids=()):
    """Docker 命令任务的常态：容器不 join，沙盒一直停在 CREATING。"""
    return {name: {'name': name, 'state': 'CREATING', 'pids': list(pids)}}


def _exploding_identity(*_args, **_kwargs):
    raise AssertionError('this request must be rejected before resolving identity')


def _never_allocated_pid() -> int:
    """永远不可能存在的 PID：内核不会分配超过 pid_max 的号。"""
    with open('/proc/sys/kernel/pid_max', encoding='utf-8') as stream:
        return int(stream.read().strip()) + 1


@pytest.fixture
def runtime_http():
    """真 Flask 路由；管理器与身份由各测试自己注入。"""
    app = Flask(__name__)
    app.register_blueprint(container_bp, url_prefix='/container')
    return app.test_client()


def _install(monkeypatch, manager, identity=None):
    """注入管理器；``identity`` 可以是身份对象，也可以是替身函数。"""
    monkeypatch.setattr(
        SbxManager, 'get_instance', classmethod(lambda cls: manager))
    if identity is not None:
        factory = identity if callable(identity) else (lambda *a, **k: identity)
        monkeypatch.setattr(
            'neu_box.api.containers.runtime_container_identity', factory)


def _register(client, **overrides):
    body = {
        'container_id': 'cid',
        'host_pid': 123,
        'sandbox_cgroup': SANDBOX,
    }
    body.update(overrides)
    return client.post('/container/register', json=body)


# ── 登记事务（SbxManager 层） ───────────────────────────────────


def test_register_container_if_free_serializes_check_and_insert():
    class DB:
        def __init__(self):
            self.record = None

        def get_container(self, _namespace):
            return self.record

        def get_sandbox(self, _name):
            return {'state': 'ACTIVE'}

    manager = SbxManager.__new__(SbxManager)
    manager.db = DB()
    manager.lock = threading.RLock()
    manager._container_registration_lock = threading.RLock()
    entered = threading.Barrier(2)
    results = []

    def register(name, identity):
        record = {'sandbox_name': name, 'mount_namespace': identity.mount_namespace}
        manager.db.record = record
        return record

    manager.register_container = register

    def worker():
        entered.wait()
        try:
            results.append(manager.register_container_if_free('sbx_u_1.slice', _identity()))
        except DockerExecutorError as exc:
            results.append(exc.code)

    # Start both callers together; only one may pass the check-and-insert
    # transaction while the other observes the first row.
    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    first.join()
    second.join()

    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert 'docker_container_already_registered' in results


def test_release_container_expected_identity_does_not_delete_replacement():
    class DB:
        def get_container(self, _namespace):
            return {
                'mount_namespace': 42,
                'sandbox_name': 'sbx_other_2.slice',
                'container_id': 'new-container',
            }

    manager = SbxManager.__new__(SbxManager)
    manager.db = DB()
    manager.lock = threading.RLock()
    manager._container_registration_lock = threading.RLock()
    manager.unbind_container = lambda _namespace: pytest.fail('unexpected unbind')
    manager._close_container_handles = lambda _namespace: pytest.fail('unexpected close')

    manager.release_container(
        42,
        expected_sandbox_name='sbx_user_1.slice',
        expected_container_id='old-container',
    )


def test_register_container_if_free_rejects_destroying_sandbox():
    class DB:
        def get_sandbox(self, _name):
            return {'state': 'DESTROYING'}

        def get_container(self, _namespace):
            return None

    manager = SbxManager.__new__(SbxManager)
    manager.db = DB()
    manager.lock = threading.RLock()
    manager._container_registration_lock = threading.RLock()
    manager.register_container = lambda *_args: pytest.fail('must not register')

    with pytest.raises(DockerExecutorError) as error:
        manager.register_container_if_free('sbx_user_1.slice', _identity())
    assert error.value.code == 'sandbox_not_active'


def test_register_runtime_container_is_idempotent_per_identity():
    manager = _manager(sandboxes=_active_sandbox())

    first, created = manager.register_runtime_container(SANDBOX, _identity())
    second, created_again = manager.register_runtime_container(SANDBOX, _identity())

    assert created is True
    assert created_again is False
    assert first == second == manager.db.containers[42]


def test_register_runtime_container_promotes_a_creating_sandbox():
    """登记即 join：容器不进 cgroup，登记就是它加入沙盒的那一步。"""
    manager = _manager(sandboxes=_creating_sandbox())

    record, created = manager.register_runtime_container(SANDBOX, _identity())

    assert created is True
    assert record == manager.db.containers[42]
    assert manager.db.get_sandbox(SANDBOX)['state'] == 'ACTIVE'
    assert manager.db.activations == [(SANDBOX, [])]


def test_register_runtime_container_keeps_the_container_pid_out_of_the_pids():
    """pids 是"谁在沙盒 cgroup 里"的记录，容器不在里面。"""
    manager = _manager(sandboxes=_creating_sandbox(pids=[4242]))

    manager.register_runtime_container(SANDBOX, _identity(host_pid=9999))

    assert manager.db.get_sandbox(SANDBOX)['pids'] == [4242]


def test_register_runtime_container_leaves_an_active_sandbox_alone():
    manager = _manager(sandboxes=_active_sandbox())

    record, created = manager.register_runtime_container(SANDBOX, _identity())

    assert created is True
    assert manager.db.get_sandbox(SANDBOX)['state'] == 'ACTIVE'
    assert manager.db.activations == []


def test_register_runtime_container_activates_only_once():
    """幂等重试不该再推一次状态（第二发看到的已经是 ACTIVE）。"""
    manager = _manager(sandboxes=_creating_sandbox())

    manager.register_runtime_container(SANDBOX, _identity())
    _, created = manager.register_runtime_container(SANDBOX, _identity())

    assert created is False
    assert manager.db.activations == [(SANDBOX, [])]


def test_register_runtime_container_rejects_sandbox_being_destroyed():
    manager = _manager(
        sandboxes={SANDBOX: {'name': SANDBOX, 'state': 'DESTROYING'}})

    with pytest.raises(DockerExecutorError) as error:
        manager.register_runtime_container(SANDBOX, _identity())
    assert error.value.code == 'sandbox_not_active'
    assert manager.db.containers == {}
    assert manager.db.activations == []


def test_register_runtime_container_rejects_namespace_of_another_sandbox():
    other = 'sbx_other_task-9.slice'
    manager = _manager(
        sandboxes={**_active_sandbox(), other: {'name': other, 'state': 'ACTIVE'}},
        containers={42: {'sandbox_name': other, 'container_id': 'cid'}},
    )

    with pytest.raises(DockerExecutorError) as error:
        manager.register_runtime_container(SANDBOX, _identity())
    assert error.value.code == 'docker_container_registered_elsewhere'


# ── 端点 ────────────────────────────────────────────────────────


def test_endpoint_registers_then_answers_already_registered(runtime_http, monkeypatch):
    manager = _manager(sandboxes=_active_sandbox())
    _install(monkeypatch, manager, _identity(namespace=9001, cgroup='/docker/abc'))

    first = _register(runtime_http, container_id='cid', host_pid=4242)
    assert first.status_code == 201
    assert first.get_json() == {
        'sandbox_name': SANDBOX,
        'container_id': 'cid',
        'mount_namespace': 9001,
        'container_cgroup': '/docker/abc',
        'status': 'registered',
    }

    again = _register(runtime_http, container_id='cid', host_pid=4242)
    assert again.status_code == 200
    assert again.get_json()['status'] == 'already_registered'


def test_endpoint_reports_unknown_sandbox(runtime_http, monkeypatch):
    _install(monkeypatch, _manager(sandboxes=_active_sandbox()), _exploding_identity)

    response = _register(runtime_http, sandbox_cgroup='sbx_someone_else_1.slice')
    assert response.status_code == 404
    assert response.get_json()['code'] == 'sandbox_not_found'


def test_endpoint_promotes_a_creating_sandbox(runtime_http, monkeypatch):
    """docker 命令任务的沙盒没人 join，端点这一层必须也能把它推成 ACTIVE。"""
    manager = _manager(sandboxes=_creating_sandbox())
    _install(monkeypatch, manager, _identity(namespace=9001, cgroup='/docker/abc'))

    response = _register(runtime_http, container_id='cid', host_pid=4242)

    assert response.status_code == 201
    assert response.get_json()['status'] == 'registered'
    assert manager.db.get_sandbox(SANDBOX)['state'] == 'ACTIVE'


def test_endpoint_reports_inactive_sandbox(runtime_http, monkeypatch):
    manager = _manager(
        sandboxes={SANDBOX: {'name': SANDBOX, 'state': 'DESTROYING'}})
    _install(monkeypatch, manager, _identity())

    response = _register(runtime_http)
    assert response.status_code == 409
    assert response.get_json()['code'] == 'sandbox_not_active'
    assert manager.db.containers == {}
    assert manager.db.activations == []


def test_endpoint_reports_namespace_registered_elsewhere(runtime_http, monkeypatch):
    manager = _manager(
        sandboxes=_active_sandbox(),
        containers={42: {'sandbox_name': 'sbx_other_task-9.slice',
                         'container_id': 'cid'}},
    )
    _install(monkeypatch, manager, _identity(namespace=42))

    response = _register(runtime_http)
    assert response.status_code == 409
    assert response.get_json()['code'] == 'docker_container_registered_elsewhere'


@pytest.mark.parametrize('body,expected', [
    ({}, 400),
    ({'container_id': 'cid'}, 400),
    ({'container_id': 'cid', 'sandbox_cgroup': SANDBOX}, 400),
    ({'container_id': '', 'host_pid': 1, 'sandbox_cgroup': SANDBOX}, 400),
    ({'container_id': 'cid', 'host_pid': 'abc', 'sandbox_cgroup': SANDBOX}, 400),
    ({'container_id': 'cid', 'host_pid': 0, 'sandbox_cgroup': SANDBOX}, 400),
    ({'container_id': 'cid', 'host_pid': -3, 'sandbox_cgroup': SANDBOX}, 400),
    # 参数就不合法时连身份都不该去解析（_exploding_identity 会炸）。
    ({'container_id': 'cid', 'host_pid': 1,
      'sandbox_cgroup': 'sbx_unknown_1.slice'}, 404),
])
def test_endpoint_rejects_bad_requests_before_resolving_identity(
        runtime_http, monkeypatch, body, expected):
    _install(monkeypatch, _manager(sandboxes=_active_sandbox()), _exploding_identity)

    response = runtime_http.post('/container/register', json=body)
    assert response.status_code == expected


@pytest.mark.parametrize('overrides,code', [
    ({'container_cgroup': '/docker/other'}, 'runtime_identity_changed'),
    ({'mount_namespace': 12345}, 'runtime_identity_changed'),
    ({'mount_namespace': 'not-an-inum'}, 'runtime_identity_changed'),
])
def test_endpoint_rejects_inconsistent_optional_observations(
        runtime_http, monkeypatch, overrides, code):
    _install(
        monkeypatch,
        _manager(sandboxes=_active_sandbox()),
        _identity(namespace=9001, cgroup='/docker/abc'),
    )

    response = _register(runtime_http, **overrides)
    assert response.status_code == 409
    assert response.get_json()['code'] == code


def test_endpoint_accepts_matching_optional_observations(runtime_http, monkeypatch):
    _install(
        monkeypatch,
        _manager(sandboxes=_active_sandbox()),
        _identity(namespace=9001, cgroup='/docker/abc'),
    )

    response = _register(
        runtime_http, container_cgroup='/docker/abc', mount_namespace=9001)
    assert response.status_code == 201
    assert response.get_json()['status'] == 'registered'


# ── 用真的 runtime_container_identity（只读 /proc） ──────────────


def test_endpoint_rejects_pid_sharing_the_host_mount_namespace(runtime_http, monkeypatch):
    """宿主机自己绝不能登记成受托方 —— 这条用真身份解析验证。"""
    _install(monkeypatch, _manager(sandboxes=_active_sandbox()))

    response = _register(runtime_http, container_id='host', host_pid=os.getpid())
    assert response.status_code == 409
    assert response.get_json()['code'] == 'docker_container_same_mount_namespace'


def test_endpoint_reports_dead_host_pid(runtime_http, monkeypatch):
    _install(monkeypatch, _manager(sandboxes=_active_sandbox()))

    response = _register(
        runtime_http, container_id='gone', host_pid=_never_allocated_pid())
    assert response.status_code == 409
    assert response.get_json()['code'] == 'docker_container_pid_invalid'


def test_endpoint_resolves_identity_without_docker(runtime_http, monkeypatch):
    """契约第 1 条：只读 /proc，不查 Docker（这里把 docker_client 换成炸弹）。"""
    _install(monkeypatch, _manager(sandboxes=_active_sandbox()))
    monkeypatch.setattr(
        containers, 'docker_client',
        lambda *args, **kwargs: pytest.fail('registration must not query Docker'))
    # 假 /proc：真 runtime_container_identity 跑完整条路径。
    monkeypatch.setattr(
        containers, 'namespace_inode',
        lambda pid, namespace: 0x1000 + int(pid))
    monkeypatch.setattr(
        containers, 'read_unified_cgroup', lambda pid: f'/docker/{int(pid)}')
    monkeypatch.setattr(containers, 'process_start_time', lambda pid: 7)

    response = _register(runtime_http, container_id='oci-1', host_pid=os.getppid())
    assert response.status_code == 201
    body = response.get_json()
    assert body['container_id'] == 'oci-1'
    assert body['mount_namespace'] == 0x1000 + os.getppid()
    assert body['container_cgroup'] == f'/docker/{os.getppid()}'
    assert body['status'] == 'registered'


# ── 契约第 3 条：状态检查与写登记在同一个临界区里 ────────────────


def test_endpoint_registers_inside_the_lifecycle_lock(runtime_http, monkeypatch):
    _install(monkeypatch, _manager(sandboxes=_active_sandbox()), _identity())
    results = []
    thread = threading.Thread(target=lambda: results.append(_register(runtime_http)))

    with SbxManager.lifecycle_lock():
        thread.start()
        # 端点只做内存操作：没被锁挡住的话这点时间早就返回了。
        time.sleep(0.2)
        blocked = results == []
    thread.join(timeout=5)

    assert blocked, 'registration did not wait for the lifecycle lock'
    assert results[0].status_code == 201
