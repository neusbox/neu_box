"""OCI runtime hook 登记流程的无特权集成测试。

覆盖的是 ``docs/container-registration.md`` 那条链路：hook 把可信的 OCI 身份 POST 给
Worker，Worker 自己从 ``/proc`` 解析真身份，登记**恰好一次**地落到一个
ACTIVE 沙盒名下。

文件里两组用例，替身范围不一样：

**第一组（端点契约）** 用假的 ``/proc`` 读取和假的 DB。它验的是"哪个输入该
回哪个状态码、什么时候绝不能写库"，输入是任意构造的，所以身份也得是构造的。
真 Flask 路由、真 ``runtime_container_identity``、真
``SbxManager.register_runtime_container``。

**第二组（生命周期与对账）** 用**真的 SQLite** 和**真的 ``/proc``**。它验的是
状态机本身：登记落库的身份是不是真的、容器退出注销、inum 复用、死条目对账、
登记与销毁并发 —— 这些对着假 DB 看等于自说自话。这里的"容器"是一个真的
长时间运行的子进程（``/proc/<pid>`` 里的 starttime、cgroup、mnt ns inum 全真），
替身只剩三处：

1. ``namespace_inode`` 只对**本进程自己**（那次"和宿主比 mnt ns"的调用）返回
   一个错开的值。契约要求拒绝与宿主机共用 mount namespace 的 PID，而一台
   普通开发机上每个进程都共用宿主 mnt ns —— 假容器也一样，不挡住这一条就
   永远是 409。替身只认 ``os.getpid()``，碰不到被测 PID 的任何取值。
2. ``bind_container`` / ``unbind_container`` —— 写 BPF map，需要 root。换成记账。
3. ``_run_native``（沙盒销毁的 native 调用）和 ``_kill_container``（``docker rm -f``）
   —— 同样需要 root / docker。前者记账，后者真的把假容器进程杀掉，好让
   ``_await_container_exit`` 走的 pidfd + epoll 那条路是真的。
"""

import os
import select
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from neu_box.api import containers as api_containers
from neu_box.api.containers import container_bp
from neu_box.migrations.engine import migrate_database
from neu_box.runtime import containers
from neu_box.runtime import sandbox as sandbox_module
from neu_box.runtime.containers import DockerExecutorError
from neu_box.runtime.reaper import Reaper
from neu_box.runtime.sandbox import SbxManager
from neu_box.storage import (
    MIGRATIONS_PACKAGE,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    Database,
)

pytestmark = pytest.mark.integration

SANDBOX = 'sbx_yuxd_task-1.slice'
OTHER_SANDBOX = 'sbx_yuxd_task-2.slice'
CONTAINER_ID = 'a' * 64
OLD_CONTAINER_ID = 'b' * 64


def _no_docker(*_args, **_kwargs):
    raise AssertionError('runtime registration must never query Docker')


class _DB:
    def __init__(self, sandboxes):
        self.sandboxes = {
            name: dict(record) for name, record in sandboxes.items()
        }
        self.containers = {}
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
        return True


def _manager(sandboxes):
    manager = SbxManager.__new__(SbxManager)
    manager.db = _DB(sandboxes)
    manager.lock = threading.RLock()
    manager._container_registration_lock = threading.RLock()

    def register_container(sandbox_name, identity):
        # native 的 bind-container 需要 BPF + root，这里只保留它的记账效果。
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


def _sandbox(name, state='ACTIVE'):
    return {name: {'name': name, 'state': state}}


@pytest.fixture
def app(monkeypatch):
    """真路由 + 假 /proc；docker_client 全程是炸弹。"""
    monkeypatch.setattr(
        containers, 'namespace_inode',
        lambda pid, namespace: 0x1000 + int(pid))
    monkeypatch.setattr(
        containers, 'read_unified_cgroup',
        lambda pid: f'/system.slice/docker-{int(pid)}.scope')
    monkeypatch.setattr(containers, 'process_start_time', lambda pid: 17)
    monkeypatch.setattr(containers, 'docker_client', _no_docker)
    application = Flask(__name__)
    application.register_blueprint(container_bp, url_prefix='/container')
    return application


def _hook_register(app, *, host_pid, sandbox_cgroup=SANDBOX,
                   container_id=CONTAINER_ID, **observations):
    body = {
        'container_id': container_id,
        'host_pid': host_pid,
        'sandbox_cgroup': sandbox_cgroup,
    }
    body.update(observations)
    return app.test_client().post('/container/register', json=body)


def _install(monkeypatch, manager):
    monkeypatch.setattr(
        SbxManager, 'get_instance', classmethod(lambda cls: manager))


def test_hook_registration_is_visible_to_the_executor(monkeypatch, app):
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    hook_pid = os.getppid()
    mount_namespace = 0x1000 + hook_pid

    response = _hook_register(
        app, host_pid=hook_pid, container_cgroup=f'/system.slice/docker-{hook_pid}.scope',
        mount_namespace=mount_namespace,
    )

    assert response.status_code == 201
    assert response.get_json() == {
        'sandbox_name': SANDBOX,
        'container_id': CONTAINER_ID,
        'mount_namespace': mount_namespace,
        'container_cgroup': f'/system.slice/docker-{hook_pid}.scope',
        'status': 'registered',
    }
    # DockerCommandExecutor._run_blocking 采纳容器时读的就是这一行。
    record = manager.db.containers[mount_namespace]
    assert record['sandbox_name'] == SANDBOX
    assert record['container_id'] == CONTAINER_ID
    assert record['init_start_time'] == 17


def test_hook_retry_is_idempotent(monkeypatch, app):
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)

    first = _hook_register(app, host_pid=os.getppid())
    second = _hook_register(app, host_pid=os.getppid())

    assert (first.status_code, first.get_json()['status']) == (201, 'registered')
    assert (second.status_code, second.get_json()['status']) == (200, 'already_registered')
    assert len(manager.db.containers) == 1


def test_concurrent_hook_retries_register_exactly_once(monkeypatch, app):
    """check-and-insert 必须在同一个临界区里，否则两发请求都会写。"""
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    hook_pid = os.getppid()
    results = []
    start = threading.Barrier(4)

    def call():
        start.wait()
        results.append(_hook_register(app, host_pid=hook_pid).get_json()['status'])

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results) == ['already_registered'] * 3 + ['registered']
    assert len(manager.db.containers) == 1


def test_hook_registration_promotes_a_creating_task_sandbox(monkeypatch, app):
    """命令任务的沙盒永远等不到 join —— 登记本身把它推成 ACTIVE。

    沙盒的 pids 不为空（4242 是个哨兵值），因为这条同时管着另一件事：
    **容器 init PID 不能污染 pids**。推状态时沿用沙盒自己的快照 ——
    抹掉（沙盒里真有人时等于把人踢出去）和加料（容器不在沙盒 cgroup 里）
    都是错的，两种错都要被这里拦住。
    """
    manager = _manager({
        SANDBOX: {'name': SANDBOX, 'state': 'CREATING', 'pids': [4242]}})
    _install(monkeypatch, manager)

    response = _hook_register(app, host_pid=os.getppid())

    assert response.status_code == 201
    assert response.get_json()['status'] == 'registered'
    assert manager.db.get_sandbox(SANDBOX)['state'] == 'ACTIVE'
    assert manager.db.activations == [(SANDBOX, [4242])]
    assert manager.db.sandboxes[SANDBOX]['pids'] == [4242]


def test_hook_rejects_sandbox_that_is_being_destroyed(monkeypatch, app):
    manager = _manager(_sandbox(SANDBOX, state='DESTROYING'))
    _install(monkeypatch, manager)

    response = _hook_register(app, host_pid=os.getppid())

    assert response.status_code == 409
    assert response.get_json()['code'] == 'sandbox_not_active'
    assert manager.db.containers == {}
    assert manager.db.activations == []


def test_hook_rejects_unknown_sandbox_name(monkeypatch, app):
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)

    response = _hook_register(
        app, host_pid=os.getppid(), sandbox_cgroup='sbx_other_9.slice')

    assert response.status_code == 404
    assert response.get_json()['code'] == 'sandbox_not_found'
    assert manager.db.containers == {}


def test_hook_rejects_annotation_pointing_at_a_live_sandbox_path(monkeypatch, app):
    """cgroup 路径 / basename 一律不认 —— 它们会被回收复用。"""
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)

    for value in (
        f'/sys/fs/cgroup/sandbox/{SANDBOX}',
        f'../{SANDBOX}',
        f'/{SANDBOX}',
    ):
        response = _hook_register(
            app, host_pid=os.getppid(), sandbox_cgroup=value)
        assert response.status_code == 404, value
        assert response.get_json()['code'] == 'sandbox_not_found'

    assert manager.db.containers == {}


def test_hook_rejects_namespace_already_bound_elsewhere(monkeypatch, app):
    manager = _manager({**_sandbox(SANDBOX), **_sandbox(OTHER_SANDBOX)})
    _install(monkeypatch, manager)
    hook_pid = os.getppid()

    assert _hook_register(app, host_pid=hook_pid).status_code == 201
    elsewhere = _hook_register(
        app, host_pid=hook_pid, sandbox_cgroup=OTHER_SANDBOX)
    # 同一个 mount namespace 换了 container_id：容器重建后旧登记还没清掉。
    rebuilt = _hook_register(app, host_pid=hook_pid, container_id='b' * 64)

    assert elsewhere.status_code == 409
    assert elsewhere.get_json()['code'] == 'docker_container_registered_elsewhere'
    assert rebuilt.status_code == 409
    assert rebuilt.get_json()['code'] == 'docker_container_registered_elsewhere'
    assert len(manager.db.containers) == 1


def test_hook_rejects_pid_in_the_host_mount_namespace(monkeypatch, app):
    """和宿主机共用 mnt ns 的 PID 等于把整机登记成受托方。"""
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    monkeypatch.setattr(
        containers, 'namespace_inode', lambda pid, namespace: 0x2000)

    response = _hook_register(app, host_pid=os.getpid())

    assert response.status_code == 409
    assert response.get_json()['code'] == 'docker_container_same_mount_namespace'
    assert manager.db.containers == {}


def test_hook_rejects_pid_that_is_already_gone(monkeypatch, app):
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    with open('/proc/sys/kernel/pid_max', encoding='utf-8') as stream:
        dead_pid = int(stream.read().strip()) + 1

    response = _hook_register(app, host_pid=dead_pid)

    assert response.status_code == 409
    assert response.get_json()['code'] == 'docker_container_pid_invalid'
    assert manager.db.containers == {}


# ── 必填参数（契约 400 那一行） ─────────────────────────────────


def _explode_identity(*_args, **_kwargs):
    raise AssertionError('这个请求必须在校验阶段就被拒掉，轮不到解析身份')


@pytest.fixture
def extra_live_pid():
    """另一个真的活着的 PID，且**不是**本进程。

    这一组的 /proc 是假的，但两件事是真的：``/proc/<pid>`` 存不存在由真
    ``os.path.exists`` 查，宿主比较用的是本进程自己的号 —— 所以"第二个容器"
    的 PID 不能是本进程，借 ``os.getppid()`` 又只有一发，起个真的短命进程。
    """
    process = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait(timeout=10)


@pytest.mark.parametrize('body,field', [
    ({'host_pid': 4242, 'sandbox_cgroup': SANDBOX}, 'container_id'),
    ({'container_id': CONTAINER_ID, 'sandbox_cgroup': SANDBOX}, 'host_pid'),
    ({'container_id': CONTAINER_ID, 'host_pid': 4242}, 'sandbox_cgroup'),
    ({}, 'container_id'),
])
def test_hook_rejects_missing_required_fields(monkeypatch, app, body, field):
    """三个必填字段缺一个就 400，而且碰都不碰身份解析和数据库。

    契约（``docs/worker-api.md`` 错误表）给 400 那一行的 ``code`` 是 ``—``：
    只有人读的 ``error``，没有机器可读的码。这里按契约断，不是按"应该有个码"
    断 —— 调用方（hook）看的是非 2xx，不看码。
    """
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    monkeypatch.setattr(
        api_containers, 'runtime_container_identity', _explode_identity)

    response = app.test_client().post('/container/register', json=body)

    assert response.status_code == 400
    answer = response.get_json()
    assert 'code' not in answer, answer
    assert field in answer['error'], answer
    assert manager.db.containers == {}
    assert manager.db.activations == []


@pytest.mark.parametrize('host_pid', [0, -1, -999, 'abc', '1.5', '', None])
def test_hook_rejects_a_useless_host_pid(monkeypatch, app, host_pid):
    """0 / 负数 / 非数字一律 400：它们在到达 /proc 之前就该被挡住。"""
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    monkeypatch.setattr(
        api_containers, 'runtime_container_identity', _explode_identity)

    response = _hook_register(app, host_pid=host_pid)

    assert response.status_code == 400, response.get_json()
    assert 'host_pid' in response.get_json()['error']
    assert manager.db.containers == {}


@pytest.mark.parametrize('overrides', [
    {'container_id': ''},
    {'container_id': '   '},
    {'container_id': '\t\n'},
    {'container_id': None},
    {'sandbox_cgroup': ''},
    {'sandbox_cgroup': '   '},
    {'sandbox_cgroup': None},
])
def test_hook_rejects_blank_required_strings(monkeypatch, app, overrides):
    """空串和全空白都算缺失 —— ``.strip()`` 之后什么都不剩，不能当名字用。

    全空白尤其重要：``'   '`` 会在库里查不到沙盒，但那是 404，等于把"参数
    没填"报成"沙盒不存在"，调用方会去查沙盒而不是查请求。
    """
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    monkeypatch.setattr(
        api_containers, 'runtime_container_identity', _explode_identity)

    response = _hook_register(app, host_pid=os.getppid(), **overrides)

    assert response.status_code == 400, response.get_json()
    assert list(overrides)[0] in response.get_json()['error']
    assert manager.db.containers == {}


# ── 可选观察值的交叉验证 ───────────────────────────────────────


def _fake_identity(pid):
    """``app`` fixture 的假 /proc 之下，这个 PID 的身份取值。"""
    return {
        'container_cgroup': f'/system.slice/docker-{int(pid)}.scope',
        'mount_namespace': 0x1000 + int(pid),
    }


@pytest.mark.parametrize('kind,expected', [
    ('omitted', 201),
    ('matching', 201),
    ('wrong_cgroup', 409),
    ('wrong_namespace', 409),
    ('namespace_not_a_number', 409),
])
def test_hook_cross_checks_the_optional_observations(
        monkeypatch, app, kind, expected):
    """hook 报上来的 cgroup / mnt ns 只做交叉验证：对得上放行，对不上 409。

    它们是**观察值，不是身份**：Worker 自己读 /proc，报错了不能带着信，也
    不能用它覆盖真值 —— 所以对不上时库和 BPF 都不许动。
    """
    manager = _manager(_sandbox(SANDBOX))
    _install(monkeypatch, manager)
    pid = os.getppid()
    truth = _fake_identity(pid)
    observations = {
        'omitted': {},
        'matching': dict(truth),
        'wrong_cgroup': {'container_cgroup': '/system.slice/docker-deadbeef.scope'},
        'wrong_namespace': {'mount_namespace': truth['mount_namespace'] + 1},
        'namespace_not_a_number': {'mount_namespace': 'not-an-inum'},
    }[kind]

    response = _hook_register(app, host_pid=pid, **observations)

    assert response.status_code == expected, response.get_json()
    if kind == 'matching':
        # 报对了也不能改变取值来源：响应里的还是 Worker 自己读到的那个。
        assert response.get_json()['container_cgroup'] == truth['container_cgroup']
        assert response.get_json()['mount_namespace'] == truth['mount_namespace']
    if expected == 409:
        assert response.get_json()['code'] == 'runtime_identity_changed'
        assert manager.db.containers == {}
        assert manager.db.activations == []
    else:
        assert len(manager.db.containers) == 1


# ── 激活只发生一次 ─────────────────────────────────────────────


def test_hook_activates_a_creating_sandbox_only_once(
        monkeypatch, app, extra_live_pid):
    """同一个沙盒登记第二个容器、或重试同一发，都不再推状态。

    ``activate_sandbox`` 只在 ``CREATING`` 那一行上成功，重推一次返回
    rowcount=0；第二发看到的已经是 ACTIVE，根本不该走到那一步。
    """
    manager = _manager(_sandbox(SANDBOX, state='CREATING'))
    _install(monkeypatch, manager)
    first_pid, second_pid = os.getppid(), extra_live_pid

    first = _hook_register(app, host_pid=first_pid)
    second = _hook_register(app, host_pid=second_pid, container_id='b' * 64)
    retry = _hook_register(app, host_pid=first_pid)

    assert [(r.status_code, r.get_json()['status'])
            for r in (first, second, retry)] == [
        (201, 'registered'), (201, 'registered'), (200, 'already_registered'),
    ]
    assert manager.db.activations == [(SANDBOX, [])]
    assert sorted(manager.db.containers) == sorted(
        [_fake_identity(first_pid)['mount_namespace'],
         _fake_identity(second_pid)['mount_namespace']])


# ══════════════════════════════════════════════════════════════════
# 真 SQLite + 真 /proc：登记落库、注销、对账、inum 复用、并发
# ══════════════════════════════════════════════════════════════════
#
# 上面那组的 DB 是手写 dict，"登记成功"等于"字典里多了个键"。这一组的对象是
# 状态机：行真的进了 SQLite、注销真的撤了授权、对账真的只清该死的那条。替身
# 只剩文档开头列的那三处，其余全真（真 Flask 路由、真事务、真 pidfd/epoll）。

#: 假容器的存活时长远大于任何单个用例：它只需要"活着"，用不着退。
_FAKE_CONTAINER = 'import time; time.sleep(600)'


def _proc_mount_namespace(pid: int) -> int:
    """自己读 /proc，别复用被测的那个函数 —— 对照物必须独立。"""
    return os.stat(f'/proc/{int(pid)}/ns/mnt').st_ino


def _proc_start_time(pid: int) -> int:
    with open(f'/proc/{int(pid)}/stat', encoding='utf-8') as stream:
        raw = stream.read().strip()
    return int(raw.rsplit(')', 1)[1].split()[19])


def _proc_cgroup(pid: int) -> str:
    with open(f'/proc/{int(pid)}/cgroup', encoding='utf-8') as stream:
        for line in stream:
            hierarchy, _controllers, path = line.rstrip('\n').split(':', 2)
            if hierarchy == '0':
                return path
    raise AssertionError(f'/proc/{pid}/cgroup 里没有 cgroup v2 记录')


class _Runtime:
    """真 DB + 真事务的运行时，附带测试侧的观察记录。"""

    def __init__(self, db, manager, application):
        self.db = db
        self.manager = manager
        self.app = application
        self.bindings = []      # ('bind', 沙盒名, inum) / ('unbind', inum)
        self.natives = []       # 走到 native helper 的命令行
        self.killed = []        # 走了 docker rm -f 的 container_ref
        self.scans = []         # 走了 Startup 容器扫描的 (沙盒名, 保留的 ref)
        self.docker_calls = []  # 真去连 Docker 的次数（登记路径必须是 0）
        self.activations = []   # activate_sandbox 被调用的次数与参数
        self.processes = {}     # container_ref -> Popen

    def client(self):
        return self.app.test_client()

    def spawn(self, container_ref):
        """起一个真的长命进程冒充容器 init。

        它的 /proc/<pid> 全是真的：mnt ns inum、cgroup、starttime。普通机器上
        所有进程共用宿主 mnt ns，所以几个"容器"报同一个 inum —— 这不是缺陷，
        正好是 ``_proc_mount_namespace`` 复用之后的样子（见复用那条用例）。
        """
        process = subprocess.Popen(
            [sys.executable, '-c', _FAKE_CONTAINER])
        self.processes[container_ref] = process
        return process

    def register(self, *, host_pid, container_ref=CONTAINER_ID,
                 sandbox_cgroup=SANDBOX, **observations):
        body = {
            'container_id': container_ref,
            'host_pid': host_pid,
            'sandbox_cgroup': sandbox_cgroup,
        }
        body.update(observations)
        return self.client().post('/container/register', json=body)

    def put_it_out_of_its_misery(self, container_ref):
        """把假容器杀掉（走的是真进程，不是 mock）。"""
        process = self.processes[container_ref]
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """真的 Worker 状态机：真 SQLite、真事务、真 /proc、真 pidfd。"""
    db_path = tmp_path / 'neu_box.db'
    migrate_database(
        db_path, MIGRATIONS_PACKAGE, REQUIRED_COLUMNS, REQUIRED_INDEXES)
    db = Database(str(db_path))
    monkeypatch.setattr(Database, '_instance', db)

    application = Flask(__name__)
    application.register_blueprint(container_bp, url_prefix='/container')

    # ── 替身 1：只把"和宿主共用 mnt ns"那一次比较错开 ────────────
    # 被测的假容器和本进程共用宿主 mnt ns，真比必然相等（而且拒绝是**对的**）。
    # 替身只认 os.getpid()，容器 PID 的 inum 照真读。
    real_namespace_inode = containers.namespace_inode

    def namespace_inode(pid, namespace):
        inum = real_namespace_inode(pid, namespace)
        if namespace == 'mnt' and int(pid) == os.getpid():
            return inum + 1   # 假的"宿主号"：真容器绝不会被分配到这个值
        return inum

    monkeypatch.setattr(containers, 'namespace_inode', namespace_inode)

    # 真的 SbxManager 状态机，绕开 __init__（它要跑 native 对账 + reaper 恢复）。
    manager = SbxManager.__new__(SbxManager)
    manager.db = db
    manager.lock = threading.RLock()
    manager._container_registration_lock = threading.RLock()
    manager._container_fds = {}
    manager._epoll = select.epoll()
    manager._allocations_paused = False
    manager._allocations_in_flight = 0

    observed = _Runtime(db, manager, application)

    # ── 替身 2：写 BPF map（要 root） ────────────────────────────
    manager.bind_container = lambda name, mount_namespace: (
        observed.bindings.append(('bind', name, int(mount_namespace))))
    manager.unbind_container = lambda mount_namespace: (
        observed.bindings.append(('unbind', int(mount_namespace))))

    # ── 替身 3：native helper（要 root）与 docker rm -f ──────────
    def run_native(*args):
        observed.natives.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')

    def kill_container(container_ref):
        """``_kill_container`` 的效果：容器真的死掉，好让后面的等待是真的。"""
        observed.killed.append(container_ref)
        process = observed.processes.get(container_ref)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    def scan_containers(sandbox_name, keep_refs=()):
        """启动时"扫一遍 Docker 找没登记的孤儿容器"：这台机器上没有 dockerd。"""
        observed.scans.append((sandbox_name, tuple(keep_refs)))
        return 0

    def docker_client(*args, **kwargs):
        observed.docker_calls.append((args, kwargs))
        raise DockerExecutorError('无法连接 Docker Engine', 'docker_unavailable')

    manager._run_native = run_native
    manager._kill_container = kill_container
    manager.remove_docker_containers_for_sandbox = scan_containers
    monkeypatch.setattr(sandbox_module, 'docker_client', docker_client)
    monkeypatch.setattr(SbxManager, '_instance', manager)
    observed.manager = manager

    real_activate = Database.activate_sandbox

    def counting_activate(self, name, pids):
        result = real_activate(self, name, pids)
        observed.activations.append((name, list(pids), result))
        return result

    monkeypatch.setattr(Database, 'activate_sandbox', counting_activate)

    try:
        yield observed
    finally:
        for process in observed.processes.values():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        for mnt_fd, pid_fd in list(manager._container_fds.values()):
            os.close(pid_fd)
            os.close(mnt_fd)
        manager._epoll.close()


def _active_sandbox(runtime, name=SANDBOX, pids=None):
    """建一个 ACTIVE 沙盒。

    建完把激活记账清零：登记一个已经 ACTIVE 的沙盒**不该**再调到
    ``activate_sandbox``，用例断的就是清零之后有没有新的调用。
    """
    runtime.db.insert_sandbox(name, cpu=2, mem='4g', pids=list(pids or []))
    assert runtime.db.activate_sandbox(name, list(pids or [])) is True
    runtime.activations.clear()
    return name


def _in_parallel(*targets, timeout=30):
    """同时开跑，收集异常；返回 (异常, 没跑完的线程)。"""
    errors = []
    start = threading.Barrier(len(targets))

    def run(target):
        try:
            start.wait(timeout=10)
            target()
        except BaseException as exc:          # noqa: BLE001 - 原样带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    return errors, [thread for thread in threads if thread.is_alive()]


def test_registration_answers_with_the_identity_it_read_from_proc(runtime):
    """成功登记的响应：五个字段齐全，每个值都是从 /proc 读出来的真值。"""
    _active_sandbox(runtime)
    process = runtime.spawn(CONTAINER_ID)

    response = runtime.register(host_pid=process.pid)

    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    # 契约（docs/worker-api.md）里这个响应就这五个键，多一个少一个都是改契约。
    assert set(body) == {
        'sandbox_name', 'container_id', 'mount_namespace',
        'container_cgroup', 'status',
    }
    assert body['sandbox_name'] == SANDBOX
    assert body['container_id'] == CONTAINER_ID
    assert body['status'] == 'registered'
    assert body['mount_namespace'] == _proc_mount_namespace(process.pid)
    assert body['container_cgroup'] == _proc_cgroup(process.pid)
    # 契约第 1 条：登记路径绝不查 Docker（hook 在 Docker 的 create 路径里）。
    assert runtime.docker_calls == []


def test_registration_persists_the_identity_read_from_proc(runtime):
    """落库的是真身份：读 ``list_containers``，与 /proc 逐项对得上。

    状态码对了但库里不是这个容器的值，等于登记没发生 —— 执行器认的是库里
    那一行，不是这次响应。
    """
    _active_sandbox(runtime)
    process = runtime.spawn(CONTAINER_ID)

    assert runtime.register(host_pid=process.pid).status_code == 201

    rows = runtime.db.list_containers(sandbox_name=SANDBOX)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row['container_id'] == CONTAINER_ID
    assert row['container_ref'] == CONTAINER_ID
    assert row['sandbox_name'] == SANDBOX
    assert row['init_host_pid'] == process.pid
    assert row['init_start_time'] == _proc_start_time(process.pid)
    assert row['mount_namespace'] == _proc_mount_namespace(process.pid)
    assert row['state'] == 'ACTIVE'
    # ACTIVE 的沙盒登记时不推状态（推的是 CREATING 那一档）。
    assert runtime.activations == []
    assert runtime.db.get_sandbox(SANDBOX)['state'] == 'ACTIVE'
    # 容器还活着：pidfd 认这一点 —— 注销路径全靠它。
    assert [record['container_id'] for record in
            runtime.manager.containers_of(SANDBOX, alive_only=True)] == [
        CONTAINER_ID]
    assert runtime.docker_calls == []


def test_release_is_idempotent_and_releases_the_pin(runtime):
    """注销幂等：同一个 mnt ns 注销两次，第二次既不报错也不盲撤一次。

    第二次盲撤是危险的：号可能已经被内核分给新容器，撤掉就是误删别人的授权
    （见 ``SbxManager._release_container_locked`` 的注释）。所以第二次必须
    静默什么都不做，而不是"再删一遍反正幂等"。
    """
    _active_sandbox(runtime)
    process = runtime.spawn(CONTAINER_ID)
    assert runtime.register(host_pid=process.pid).status_code == 201
    mount_namespace = _proc_mount_namespace(process.pid)
    assert mount_namespace in runtime.manager._container_fds

    runtime.put_it_out_of_its_misery(CONTAINER_ID)
    # 收尸线程坐的位置：epoll 上等容器退出（pidfd 是真的）。
    assert runtime.manager.wait_container_events(timeout=10) == 1

    assert runtime.db.list_containers() == []
    assert runtime.bindings == [
        ('bind', SANDBOX, mount_namespace), ('unbind', mount_namespace)]
    assert runtime.manager._container_fds == {}

    # 第二发：同一 mnt ns 再注销一次。
    runtime.manager.release_container(mount_namespace)
    assert runtime.manager.wait_container_events(timeout=0.2) == 0
    assert runtime.db.list_containers() == []
    assert runtime.bindings == [
        ('bind', SANDBOX, mount_namespace), ('unbind', mount_namespace)]


def test_reconcile_clears_only_registrations_whose_container_is_gone(runtime):
    """死条目对账：容器没了、fd 也没了（Worker 重启过）时清掉残留记录。

    服务正常时注销走 pidfd 事件，这里兜的是"事件没走到"的情况。两半都要验：
    活着的容器不能被扫掉（不然对账等于随机撤授权），死了的必须被清掉并撤授权
    （残留条目会让后来复用这个号的容器白捡一份）。
    """
    _active_sandbox(runtime)
    process = runtime.spawn(CONTAINER_ID)
    assert runtime.register(host_pid=process.pid).status_code == 201
    mount_namespace = _proc_mount_namespace(process.pid)

    # ① 容器活着：对账必须原样放过。
    assert runtime.manager.reconcile_containers() == 0
    assert [row['container_id'] for row in runtime.db.list_containers()] == [
        CONTAINER_ID]
    assert runtime.bindings == [('bind', SANDBOX, mount_namespace)]

    # ② 容器没了，且 fd 全丢（Worker 重启后就是这个样子）。
    runtime.put_it_out_of_its_misery(CONTAINER_ID)
    runtime.manager._close_container_handles(mount_namespace)
    assert runtime.manager._container_fds == {}

    assert runtime.manager.reconcile_containers() == 1
    assert runtime.db.list_containers() == []
    assert runtime.bindings == [
        ('bind', SANDBOX, mount_namespace), ('unbind', mount_namespace)]

    # 再对一次没有新东西可清（幂等）。
    assert runtime.manager.reconcile_containers() == 0


def test_a_reused_mount_namespace_does_not_inherit_the_old_registration(runtime):
    """inum 复用：残留旧记录 + 新容器同号 —— 新容器绝不能被当成"已登记"。

    内核把号收回去分给新容器这件事用户态造不出来：这台机器上所有进程共用宿主
    mnt ns，所以两个假容器天然报同一个号，正好就是复用之后的样子。要验的是
    Worker 拿到一个"已经被登记过"的号时怎么反应：

      - 库里那行不是这个容器 → fail-closed（409），不许静默当成幂等命中，
        更不许拿旧记录里的身份当它的身份；
      - 对账把旧授权撤掉（不然新容器在那个号上白捡一份），之后新容器自己登记。
    """
    _active_sandbox(runtime)
    old = runtime.spawn(OLD_CONTAINER_ID)
    assert runtime.register(
        host_pid=old.pid, container_ref=OLD_CONTAINER_ID).status_code == 201
    mount_namespace = _proc_mount_namespace(old.pid)

    # 旧容器死了，但记录还在（收尸/对账还没轮到它）。
    runtime.put_it_out_of_its_misery(OLD_CONTAINER_ID)
    assert [row['container_id'] for row in runtime.db.list_containers()] == [
        OLD_CONTAINER_ID]

    new = runtime.spawn(CONTAINER_ID)
    assert _proc_mount_namespace(new.pid) == mount_namespace, '前提：号被复用了'

    rejected = runtime.register(host_pid=new.pid)
    assert rejected.status_code == 409, rejected.get_json()
    assert rejected.get_json()['code'] == 'docker_container_registered_elsewhere'
    # 库里那行还是旧的：新容器没有被写进去，也没给它绑过授权。
    assert [row['container_id'] for row in runtime.db.list_containers()] == [
        OLD_CONTAINER_ID]
    assert runtime.bindings == [('bind', SANDBOX, mount_namespace)]

    # 对账撤掉旧授权，新容器随后自己登记，落库的是它自己的身份。
    assert runtime.manager.reconcile_containers() == 1
    assert runtime.bindings == [
        ('bind', SANDBOX, mount_namespace), ('unbind', mount_namespace)]

    accepted = runtime.register(host_pid=new.pid)
    assert accepted.status_code == 201, accepted.get_json()
    rows = runtime.db.list_containers(sandbox_name=SANDBOX)
    assert [(row['container_id'], row['init_host_pid']) for row in rows] == [
        (CONTAINER_ID, new.pid)]


def test_registration_and_destruction_do_not_deadlock(runtime, monkeypatch):
    """一边登记一边 ``destroy_sandbox``：不互等、不死锁，收尾状态自洽。

    两条路径要的锁是同一组（``self.lock → lifecycle_lock``），而且销毁那条还
    要在锁里收容器。互等的形态是两边各拿一把往下等 —— 那样这两个线程都回不来，
    所以"两个都跑完"本身就是主要断言。

    两种交错都摆出来，因为坏掉的方式只在其中一种里出现：

    ① **登记先拿到锁**（销毁在门外等）。这个顺序用事件卡出来，不是靠调度碰
       运气：登记进到临界区中间（``_promote_on_registration`` 那一刻）停住，
       这时才起销毁线程 —— 它只能等。放行之后，销毁必须把刚登记进去的容器
       一并收掉（撤授权 → ``docker rm -f`` → 等退出 → 放 pin）。
    ② **销毁先拿到锁**。登记必须在锁里重新看一次沙盒状态，而不是信进门前那
       次 404 检查的结果。

    两种都不许留下"沙盒没了、容器行还在"的分叉。

    这条测不到什么：``destroy_sandbox`` 里那段 TODO 说的互等是"销毁等 runc
    起容器、容器里的 hook 等锁"（要真 runc），这里没有 runc，测的是 Worker
    侧这两条路径之间的锁序。真正把环闭上的那一半 —— 收容器期间锁**不在**
    销毁线程手里 —— 由
    ``test_retiring_containers_does_not_hold_the_registration_locks`` 直接
    卡在收容器的等待点上验证。
    """
    # ① 登记先拿到锁：把登记卡在临界区里，销毁线程在锁外等。
    runtime.db.insert_sandbox(SANDBOX)
    process = runtime.spawn(CONTAINER_ID)
    inside = threading.Event()
    go_on = threading.Event()
    real_promote = SbxManager._promote_on_registration

    def stalled_promote(self, sandbox_name, sandbox):
        """登记此刻**已经在临界区里**（两把锁都握着）。"""
        inside.set()
        assert go_on.wait(timeout=10), '用例自己卡住了'
        return real_promote(self, sandbox_name, sandbox)

    monkeypatch.setattr(SbxManager, '_promote_on_registration', stalled_promote)
    response = {}
    destroyed = {}

    registration = threading.Thread(
        target=lambda: response.update(
            result=runtime.register(host_pid=process.pid)))
    registration.start()
    assert inside.wait(timeout=10), '登记没能进到临界区'
    # 销毁会把容器杀掉，/proc 就没了 —— 号先取下来。
    mount_namespace = _proc_mount_namespace(process.pid)

    def destroy():
        destroyed.update(result=runtime.manager.destroy_sandbox(SANDBOX))

    destroyer = threading.Thread(target=destroy)
    destroyer.start()
    # 给销毁线程一点真去抢锁的时间：抢到了它第一件事就是把沙盒标成
    # DESTROYING，所以"状态还是 CREATING"就是它被挡在锁外的实证。
    time.sleep(0.5)
    assert destroyer.is_alive(), '销毁没被登记挡住 —— 锁没起作用'
    assert runtime.db.get_sandbox(SANDBOX)['state'] == 'CREATING'

    go_on.set()
    registration.join(timeout=30)
    destroyer.join(timeout=30)
    assert not (registration.is_alive() or destroyer.is_alive()), \
        '登记与销毁互等（死锁）'

    assert response['result'].status_code == 201, response['result'].get_json()
    assert destroyed['result'] is True
    # 登记落在了一个真存在的沙盒上，随后被销毁一并收掉：撤授权、杀容器、等它
    # 真的退出、放 pin，最后沙盒行和容器行都不在。
    assert runtime.killed == [CONTAINER_ID]
    assert runtime.scans == [(SANDBOX, (CONTAINER_ID,))]
    assert runtime.bindings == [
        ('bind', SANDBOX, mount_namespace), ('unbind', mount_namespace)]
    assert runtime.manager._container_fds == {}
    assert process.poll() is not None
    assert runtime.db.get_sandbox(SANDBOX) is None
    assert runtime.db.list_containers() == []
    assert ('destroy', SANDBOX) in runtime.natives

    # ② 销毁先拿到锁：同时开跑，登记只能撞上"沙盒正在销毁 / 已经没了"。
    #
    # 两个状态码都合法，取决于销毁走到哪一步：销毁是**先把状态落成
    # DESTROYING、放锁收容器、再回来拆 native**，所以登记抢到锁时读到的既
    # 可能是 DESTROYING（409），也可能是拆完之后的"行已删除"（404）。断言
    # 只钉住"被拒"这件事 —— 钉死其中一个码就等于钉死了收容器的耗时。
    runtime.db.insert_sandbox(OTHER_SANDBOX)
    other = runtime.spawn(OLD_CONTAINER_ID)
    second = {}

    errors, stuck = _in_parallel(
        lambda: second.update(result=runtime.register(
            host_pid=other.pid, container_ref=OLD_CONTAINER_ID,
            sandbox_cgroup=OTHER_SANDBOX)),
        lambda: runtime.manager.destroy_sandbox(OTHER_SANDBOX),
    )

    assert errors == [], errors
    assert stuck == [], '登记与销毁互等（死锁）'
    assert second['result'].status_code in {404, 409}, second['result'].get_json()
    assert second['result'].get_json()['code'] in {
        'sandbox_not_found', 'sandbox_not_active'}
    assert runtime.db.get_sandbox(OTHER_SANDBOX) is None
    # 全程只有 ① 那个容器被绑过/撤过：② 的登记没留下任何痕迹。
    assert runtime.killed == [CONTAINER_ID]
    assert runtime.bindings == [
        ('bind', SANDBOX, mount_namespace), ('unbind', mount_namespace)]
    assert runtime.db.list_containers() == []


# ── 收容器期间的锁（启动窗口互等） ──────────────────────────────
#
# ``destroy_sandbox`` 的环长在"收容器"里：正在 ``runc create`` 的容器，它的
# runtime hook 要拿 ``self.lock → lifecycle_lock`` 才登记得上，而收容器会
# 停在那里等这个容器（``docker rm -f`` 排在 Docker 的容器状态锁后面，之后还
# 要等 init 真的退出）。所以收容器必须在锁外做。


class _FakeContainer:
    """假的 docker 容器对象：按 label 扫容器那两条路径用得到的字段都在
    （``id`` / ``name`` / ``attrs['Created']`` / label）。

    label 存进 ``attrs['Config']['Labels']``，和 docker-py 的内部形状一致 ——
    真实对象的 ``Container.labels`` 属性读的就是这里。
    """

    def __init__(self, container_id, created, labels=None):
        self.id = container_id
        self.name = container_id
        self.attrs = {
            'Created': created,
            'Config': {'Labels': dict(labels or {})},
        }
        self.removed = False

    def remove(self, force=False):
        assert force is True, '销毁路径必须 force'
        self.removed = True


class _FakeDocker:
    """够 ``containers.list(all=True, filters=...)`` 用的最小 client。"""

    def __init__(self, containers):
        self.listed = []
        self.closed = False
        outer = self

        class _Containers:
            def list(self, all=False, filters=None):   # noqa: A002 - docker-py 签名
                outer.listed.append((all, dict(filters or {})))
                return list(containers)

        self.containers = _Containers()

    def close(self):
        self.closed = True


def _docker_timestamp(age_seconds: float) -> str:
    """Docker 的 ``attrs['Created']`` 格式：RFC3339 **纳秒** + ``Z``。

    9 位小数和结尾的 ``Z`` 都是 Docker 真给出来的形状（``/containers/json``
    的 ``Created``）。用例拿真形状喂解析器，而不是喂一个"刚好能被
    ``fromisoformat`` 吃下"的形状 —— 后者测的是一个不存在的输入。
    """
    moment = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return moment.strftime('%Y-%m-%dT%H:%M:%S') + '.123456789Z'


def test_destroy_defers_containers_inside_the_startup_window(runtime, monkeypatch):
    """启动窗口里的容器不许碰：跳过它、保留沙盒、下一轮再来。

    窗口内（``Created`` 还很新）的容器可能正卡在 ``runc create`` 的 hook 上等
    锁，销毁去 ``docker rm -f`` 就是给自己造环。窗口外的（崩溃残留）照旧直接删。

    环真正出现的地方是"沙盒里已经有一个登记过的容器、又有一个正在创建"：
    前者让销毁必须去扫 Docker（否则扫都不扫，见 ``remove_docker_containers_
    for_sandbox`` 的快速返回），扫到后者就撞上窗口。
    """
    # 让 ``remove_docker_containers_for_sandbox`` 用真身（fixture 里换成了
    # 一个只记账的替身 —— 这台机器上没有 dockerd）。
    del runtime.manager.remove_docker_containers_for_sandbox

    young = _FakeContainer(OLD_CONTAINER_ID, _docker_timestamp(1.0))
    fake = _FakeDocker([young])
    monkeypatch.setattr(sandbox_module, 'docker_client', lambda **kwargs: fake)

    # ① 直接扫：窗口内的容器一个都不许删，且必须报"没收干净"。
    assert runtime.manager.remove_docker_containers_for_sandbox(SANDBOX) is None
    assert young.removed is False, '启动窗口里的容器被删了 —— 那是互等的入口'
    assert fake.closed is True

    # ② 销毁一个"已登记容器 + 正在创建容器"的沙盒：必须整个推迟。
    sandbox = _active_sandbox(runtime)
    container = runtime.spawn(CONTAINER_ID)
    assert runtime.register(host_pid=container.pid).status_code == 201

    assert runtime.manager.destroy_sandbox(sandbox) is False
    assert young.removed is False
    assert runtime.killed == [], '收容器没成，已登记的容器不该被单方面收掉'
    assert ('destroy', sandbox) not in runtime.natives, \
        '收容器没收干净就不许往下拆 native'
    # 落下的 DESTROYING 标记是重试的把手：收尸只看这个状态就会再来一轮。
    assert runtime.db.get_sandbox(sandbox)['state'] == 'DESTROYING'
    assert [row['container_id'] for row in runtime.db.list_containers()] == [
        CONTAINER_ID]

    # ③ 同一个容器老了（下一轮的常态）：正常删，销毁走完。
    aged = _FakeContainer(OLD_CONTAINER_ID, _docker_timestamp(3600.0))
    monkeypatch.setattr(
        sandbox_module, 'docker_client', lambda **kwargs: _FakeDocker([aged]))

    assert runtime.manager.destroy_sandbox(sandbox) is True
    assert aged.removed is True
    assert runtime.killed == [CONTAINER_ID]
    assert ('destroy', sandbox) in runtime.natives
    assert runtime.db.get_sandbox(sandbox) is None
    assert runtime.db.list_containers() == []


def test_orphan_sweep_removes_containers_whose_sandbox_is_gone(
        runtime, monkeypatch):
    """崩溃窗口的兜底：label 在、沙盒记录不在的容器必须被收掉。

    这类容器没有 ``containers`` 行，``reconcile_containers`` 看不见；沙盒记录
    被销毁路径删掉之后，按名字扫的那条路也再没有入口。全局清扫反过来按 label
    查，只看 DB 里已经不存在的沙盒名。
    """
    gone = _FakeContainer(
        OLD_CONTAINER_ID, _docker_timestamp(3600.0),
        labels={'neu-box.sandbox': 'sbx_yuxd_task-9.slice'},
    )
    live = _FakeContainer(
        'c' * 64, _docker_timestamp(3600.0),
        labels={'neu-box.sandbox': SANDBOX},
    )
    fake = _FakeDocker([gone, live])
    monkeypatch.setattr(sandbox_module, 'docker_client', lambda **kwargs: fake)
    _active_sandbox(runtime, SANDBOX)

    assert runtime.manager.reap_orphan_labelled_containers() == 1
    assert gone.removed is True, '沙盒记录已经没了的容器没被收掉'
    assert live.removed is False, '沙盒记录还在的容器不归全局清扫管'
    assert fake.closed is True


def test_orphan_sweep_defers_containers_inside_the_startup_window(
        runtime, monkeypatch):
    """窗口内的无主容器不许碰：它可能正卡在 hook 上等锁，延后到下一轮。"""
    young = _FakeContainer(
        OLD_CONTAINER_ID, _docker_timestamp(1.0),
        labels={'neu-box.sandbox': 'sbx_yuxd_task-9.slice'},
    )
    fake = _FakeDocker([young])
    monkeypatch.setattr(sandbox_module, 'docker_client', lambda **kwargs: fake)

    assert runtime.manager.reap_orphan_labelled_containers() is None
    assert young.removed is False
    assert fake.closed is True


def test_orphan_sweep_is_a_noop_when_docker_is_unreachable(runtime):
    """dockerd 不可用时清扫只回 ``None`` 等下一轮，不阻塞任何销毁。"""
    # fixture 已经把 ``docker_client`` 换成"连不上"的替身。
    assert runtime.manager.reap_orphan_labelled_containers() is None


def test_container_start_grace_survives_docker_timestamp_formats():
    """窗口判定只认时间戳，几种格式都得读得出来。"""
    parse = SbxManager._parse_docker_timestamp
    now = datetime.now(timezone.utc).timestamp()

    # Docker 的真格式：纳秒小数 + Z。
    assert abs(parse(_docker_timestamp(0)) - now) < 60
    # 读不出来的串：交给"按不在窗口里处理"（旧行为），不抛异常。
    assert parse('') is None
    assert parse(None) is None
    assert parse('不是时间') is None

    # 没有时区的串必须按 UTC 解释。这条要在**非 UTC** 的时区下断，否则
    # "按本地时区解释"和"按 UTC 解释"恰好一样，测了等于没测（开发机多数是
    # UTC，CI 不一定）。这里临时把进程时区扳到 UTC+8。
    original = os.environ.get('TZ')
    os.environ['TZ'] = 'Asia/Shanghai'
    time.tzset()
    try:
        assert parse('2026-09-14T10:00:00.123456789') == datetime(
            2026, 9, 14, 10, 0, 0, 123456, tzinfo=timezone.utc).timestamp()
    finally:
        if original is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = original
        time.tzset()


def test_retiring_containers_does_not_hold_the_registration_locks(
        runtime, monkeypatch):
    """收容器卡住时，登记必须还能进来 —— 这就是那个环的解。

    收容器是整条销毁路径里唯一会**等**容器的部分。把等的那一步钉住，然后从
    另一个线程发一次登记：它必须在收容器还卡着的时候就有结果（本例是被拒，
    因为沙盒已经是 DESTROYING）。老代码把收容器放在 ``with self.lock,
    self._lifecycle_lock`` 里，登记线程会一直等在锁上 —— 真机上等的就是正在
    ``runc create`` 的那个容器，两边都不回来。
    """
    sandbox = _active_sandbox(runtime)
    container = runtime.spawn(CONTAINER_ID)
    assert runtime.register(host_pid=container.pid).status_code == 201

    inside = threading.Event()
    go_on = threading.Event()
    real_await = SbxManager._await_container_exit

    def stalled_await(self, records):
        inside.set()
        assert go_on.wait(timeout=30), '用例自己卡住了'
        return real_await(self, records)

    monkeypatch.setattr(SbxManager, '_await_container_exit', stalled_await)

    destroyed = {}
    destroyer = threading.Thread(
        target=lambda: destroyed.update(
            result=runtime.manager.destroy_sandbox(sandbox)),
        daemon=True)
    destroyer.start()
    assert inside.wait(timeout=10), '销毁没走到等容器那一步'

    # 收容器已经卡住了。此刻：状态是 DESTROYING，锁**不在**销毁线程手里。
    assert runtime.db.get_sandbox(sandbox)['state'] == 'DESTROYING'
    other = runtime.spawn(OLD_CONTAINER_ID)
    rejected = {}
    registration = threading.Thread(
        target=lambda: rejected.update(
            result=runtime.register(
                host_pid=other.pid, container_ref=OLD_CONTAINER_ID)),
        daemon=True)
    registration.start()
    registration.join(timeout=10)
    assert not registration.is_alive(), \
        '收容器还卡着，登记却进不来 —— 销毁持着登记要的锁在等容器（互等）'
    assert rejected['result'].status_code in {404, 409}, \
        rejected['result'].get_json()
    assert rejected['result'].get_json()['code'] in {
        'sandbox_not_found', 'sandbox_not_active'}

    go_on.set()
    destroyer.join(timeout=30)
    assert not destroyer.is_alive(), '销毁自己没回来'
    assert destroyed['result'] is True
    # 销毁照旧走完：容器被杀、行清干净。
    assert runtime.killed == [CONTAINER_ID]
    assert runtime.db.get_sandbox(sandbox) is None
    assert runtime.db.list_containers() == []
    assert ('destroy', sandbox) in runtime.natives


def test_destroy_skips_a_sandbox_recreated_while_retiring_containers(
        runtime, monkeypatch):
    """收容器期间名字被重建：那是另一个沙盒，绝不许替它拆 native。

    销毁路径中间有一段不持锁。同名沙盒完全可能在这段时间里被别的销毁流程
    放掉、又被 ``create_sandbox`` 建起来 —— 重建出来的沙盒带着新一批进程和
    设备，替它跑 native destroy 就是拆掉一个活着的沙盒。
    """
    sandbox = _active_sandbox(runtime)
    container = runtime.spawn(CONTAINER_ID)
    assert runtime.register(host_pid=container.pid).status_code == 201
    first_generation = runtime.db.get_sandbox(sandbox)['created_at']

    inside = threading.Event()
    go_on = threading.Event()
    real_await = SbxManager._await_container_exit

    def stalled_await(self, records):
        inside.set()
        assert go_on.wait(timeout=30), '用例自己卡住了'
        return real_await(self, records)

    monkeypatch.setattr(SbxManager, '_await_container_exit', stalled_await)

    destroyed = {}
    destroyer = threading.Thread(
        target=lambda: destroyed.update(
            result=runtime.manager.destroy_sandbox(sandbox)),
        daemon=True)
    destroyer.start()
    assert inside.wait(timeout=10), '销毁没走到等容器那一步'

    # 就在这段窗口里：同名沙盒被放掉、又被重建。
    runtime.db.delete_sandbox(sandbox)
    time.sleep(0.01)          # created_at 是 time.time()，错开一个刻度
    runtime.db.insert_sandbox(sandbox, cpu=2, mem='4g')
    assert runtime.db.get_sandbox(sandbox)['created_at'] != first_generation

    go_on.set()
    destroyer.join(timeout=30)
    assert not destroyer.is_alive()

    # 重建出来的沙盒必须完好：native 没被动过，行还在。
    assert destroyed['result'] is True
    assert ('destroy', sandbox) not in runtime.natives, \
        '替一个刚重建的沙盒跑了 native destroy'
    assert runtime.db.get_sandbox(sandbox) is not None


# ── 只有 native 状态文件的残留 ──────────────────────────────────
#
# 沙盒有三份记录：DB 行、cgroup 目录、native 的 owner 状态文件
# （``/run/neu-box/sandbox-state/cgroup_id_<name>``，见 native/sandbox/src/state.cpp）。
# 前两份都不在时，"还有第三份"的残留会被 ``destroy_sandbox`` 的开头当成
# "本来就不存在"直接返回 True —— native destroy 根本不会被调用，状态文件里的
# 设备预留永远留着；而 ``native list`` 把状态文件算进沙盒清单，收尸于是每轮
# 都看见它、每轮都"清干净"，那张卡一直算被占着。


def _native_state(name: str) -> str:
    return os.path.join(
        sandbox_module.NATIVE_STATE_DIRECTORY,
        sandbox_module.NATIVE_STATE_PREFIX + name,
    )


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """把 native 的状态目录挪到 tmp_path（真机上是 /run/neu-box/sandbox-state）。"""
    directory = tmp_path / 'sandbox-state'
    directory.mkdir()
    monkeypatch.setattr(
        sandbox_module, 'NATIVE_STATE_DIRECTORY', str(directory))
    return directory


def _write_native_state(name: str) -> str:
    path = _native_state(name)
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write('4026533000\n')      # native 侧存的是 cgroup ID
    return path


def test_destroy_cleans_a_residue_that_only_native_knows_about(runtime, state_dir):
    """DB 行、cgroup 目录都不在，只有 native 状态文件在：必须真去 destroy。"""
    assert os.path.isdir(runtime.manager._cg_path(SANDBOX)) is False
    assert runtime.db.get_sandbox(SANDBOX) is None
    path = _write_native_state(SANDBOX)
    assert runtime.manager._native_state_exists(SANDBOX) is True

    assert runtime.manager.destroy_sandbox(SANDBOX) is True
    assert ('destroy', SANDBOX) in runtime.natives, \
        '只有状态文件的残留被当成"本来就不存在"，native 从没被调用'
    # Python 不碰这个文件：清状态、放设备预留是 native destroy 的活
    # （native/sandbox/src/main.cpp 的 destroy_one）。
    assert os.path.lexists(path) is True
    assert runtime.db.get_sandbox(SANDBOX) is None


def test_destroy_stays_a_noop_when_no_record_exists_anywhere(runtime, state_dir):
    """三份记录都不在：照旧直接返回 True，不去碰 native。"""
    assert runtime.manager._native_state_exists(SANDBOX) is False
    assert runtime.manager.destroy_sandbox(SANDBOX) is True
    assert runtime.natives == []


def test_reaper_retries_the_residue_until_native_really_cleaned_it(
        runtime, state_dir, monkeypatch):
    """收尸视角：``native list`` 看得见这份残留，销毁就该真去清掉它。

    ``native list`` 是 cgroup 目录名和状态文件名的并集（native/sandbox/src/
    main.cpp），所以只剩状态文件的沙盒在收尸眼里和正常沙盒一样"存在"。这里
    让 ``list`` 如实报出这个名字，并让 ``destroy`` 按 native 的语义真的把状态
    文件删掉 —— 于是"清干净"这件事必须是**真的**发生，而不是回一句成功。
    """
    path = _write_native_state(SANDBOX)
    real_native = runtime.manager._run_native
    # 收尸器不在 fixture 里（``__init__`` 会真去跑 native 对账）；这里现搭一个。
    # ``_native_path`` 有值才会走 ``native list``，否则退化成扫 /sys/fs/cgroup。
    runtime.manager.reaper = Reaper(runtime.manager)
    monkeypatch.setattr(
        runtime.manager, '_native_path', 'native-helper-替身', raising=False)

    def native(*args):
        # 只对本用例的沙盒作数：``native list`` 还会并上真实 /sys/fs/cgroup 里
        # 扫出来的名字，这台机器上可能就有别人的残留。
        if args[0] == 'list':
            # native list：cgroup 里的名字 ∪ 状态文件里的名字。这个残留只有
            # 后一半，所以"native list 看得见"本身就是它被反复收尸的原因。
            return subprocess.CompletedProcess(args, 0, f'{SANDBOX}\n', '')
        if args[0] == 'destroy' and args[1] == SANDBOX:
            # native destroy 的语义（main.cpp 的 destroy_one）：没有活 cgroup
            # 但有 owner 状态时，放掉设备预留并删掉状态文件。
            if os.path.lexists(path):
                os.unlink(path)
        return real_native(*args)

    monkeypatch.setattr(runtime.manager, '_run_native', native)

    assert runtime.manager.reaper.run_once() >= 1
    assert os.path.lexists(path) is False, '收尸报成功，残留却还在'
    assert ('destroy', SANDBOX) in runtime.natives
    # 清干净之后就该安静下来：下一轮不再把它当孤儿发现。
    runtime.natives.clear()
    runtime.manager.reaper.run_once()
    assert ('destroy', SANDBOX) not in runtime.natives
