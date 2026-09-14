"""跨仓库集成：真的 ``neu-box-hook`` 二进制 → 真的 Worker HTTP → 真的数据库。

这一层要回答的问题只有一句话：**hook 实际发的请求，Worker 实际收不收**。
两边各自的单元测试都只对着自己写的假对象 —— runtime 那边是 httptest 假服务器
（只验"我发了什么"），Worker 这边是手写 dict（只验"我能收什么"）。annotation
键名（``sandbox_cgroup`` vs ``neu-box.sandbox-cgroup``）和取值语义（沙盒名 vs
cgroup 路径）这两处已经因此各出过一次 bug，都是靠人读代码发现的。

和 ``test_runtime_flow.py``（第 1 层，手写 dict 打进 Flask test client）的区别：
这里没有 test client，也没有手写的请求体 —— 请求是 ``neu-box-hook`` 这个独立
进程真的发出来的，worker 是 ``werkzeug.serving`` 真的在监听的一个端口。

被替身换掉的只有三处，其余全真（真进程、真 HTTP、真请求体解析、真 SQLite 写入）:

1. ``neu_box.api.containers.runtime_container_identity`` —— **/proc 那一层**。
   契约要求 Worker 拒绝与宿主机共用 mount namespace 的 PID，而一台普通开发机
   上所有进程都共用宿主 mnt ns，真读 /proc 必然被正确地拒掉
   （``docker_container_same_mount_namespace``）。替身仍然真读 ``/proc``（mnt ns
   inum、cgroup、starttime 全是真的），只把"和宿主同名就拒"这一条让开。
2. ``SbxManager.bind_container`` / ``unbind_container`` —— **写 BPF map 那一层**，
   需要 native helper + root。替身只记账，断言落在"登记确实以正确的沙盒名和
   mnt ns 走到了写 map 这一步"。
3. ``SbxManager.open_container_handles`` —— **pidfd 那一层**。mnt ns fd 照真的开
   （返回的 inum 就是 BPF 认的那个数，``register_container`` 里那个"身份在登记
   前后没变"的断言照跑），只有 pidfd 换成一个永不触发的占位 fd：``os.pidfd_open``
   在 Anaconda 打包的解释器里**不存在**（本仓库的 ``.venv`` 就是，系统
   ``/usr/bin/python3.11`` 有），而容器退出监听不是这里要验的东西。
   真机上这条路径必须有 ``os.pidfd_open``，见文末「已知缺口」。

文件里有两组用例，区别只在对面那个 Worker 是真是假：

* **真 Worker**（上面那三处替身）：契约字段、幂等、404、连不上、annotation 回退
  —— 这些要的是"真的写进数据库了没有"，Worker 必须是真的。
* **假 Worker**（``fake_worker`` fixture，一个真的 ``http.server``，不加载 Worker
  代码）：5xx / 4xx / 挂着不响应 / 坏 stdin。这些验的是 **hook 那一侧**的行为
  —— 退非零、只发一次、把原因写进 stderr、在 8s HTTP 超时内自己退而不是被 runc
  杀掉。真 Worker 造不出这些：它不会平白返 500，而"沙盒正在销毁"这种 409 要造
  出来又得先把状态推到 DESTROYING，验的仍然是 Worker 的判断，不是 hook 的。

fixture 只检查 hook 二进制在不在，**绝不编译任何东西**：找不到就 skip。

已知缺口（这些测试顺手发现的，不在本文件修）:

1. 真的 ``open_container_handles`` 在缺 ``os.pidfd_open`` 的解释器上抛
   ``AttributeError``，端点于是 500 —— 也就是说 Worker 跑在那种解释器下
   **登记不了任何容器**。现有测试全都把 ``register_container`` 整个换成替身，
   所以这一层没有覆盖。装机时用的解释器要确认有 ``os.pidfd_open``。

（原先还有一条：``create_app()`` 没有关掉 Flask 的 ``ensure_ascii``，契约里的中文
error 文本被转义成 ``\\uXXXX`` 才进 hook 的 stderr。已在 ``app.py`` 关掉，并由
``test_registration_rejection_is_not_ascii_escaped`` 看着。）
"""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import flask
import pytest
from werkzeug.serving import make_server

from neu_box.api import containers as api_containers
from neu_box.app import create_app
from neu_box.migrations.engine import migrate_database
from neu_box.runtime.containers import (
    ContainerIdentity,
    mount_namespace_of,
    process_start_time,
    read_unified_cgroup,
)
from neu_box.runtime.sandbox import SbxManager
from neu_box.storage import (
    MIGRATIONS_PACKAGE,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    Database,
)

pytestmark = pytest.mark.integration

SANDBOX = 'sbx_yuxd_task-hook.slice'
UNKNOWN_SANDBOX = 'sbx_yuxd_task-nope.slice'
PLACEHOLDER_SANDBOX = 'sbx_yuxd_task-bundle.slice'
CONTAINER_ID = 'c' * 64

HOOK_ENV = 'NEU_BOX_HOOK_BIN'
SYSTEM_HOOK = '/usr/local/bin/neu-box-hook'

# 契约里这个端点收的全部字段：三个必填 + 两个可选的交叉验证值。
# 键名漂了（多一个不认识的键）这里就该失败。
CONTRACT_FIELDS = frozenset({
    'container_id', 'host_pid', 'sandbox_cgroup',
    'container_cgroup', 'mount_namespace',
})


# ══════════════════════════════════════════════════════════════════
# hook 二进制：只查在不在
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def hook_binary():
    """真的 ``neu-box-hook``。找不到就 skip —— 这里不编译任何东西。"""
    candidates = (os.environ.get(HOOK_ENV, '').strip(), SYSTEM_HOOK)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    pytest.skip(
        '没找到 neu-box-hook 二进制（设 NEU_BOX_HOOK_BIN 或装 runtime）；'
        f'{HOOK_ENV}={os.environ.get(HOOK_ENV) or "<未设置>"}，'
        f'{SYSTEM_HOOK} 不存在。这个 fixture 不会去编译它。'
    )


def _run_hook(hook_binary, worker_url, state, timeout=30, stdin_text=None):
    """按 runc 的方式拉起 hook：stdin 喂 OCI state，返回值看退出码。

    超时给到 30s 远大于 hook 自己的 10s 预算，卡住的话是测试报错而不是假通过。
    ``stdin_text`` 是"喂不进 JSON"那类用例的入口：直接给文本，不做序列化。
    """
    env = dict(os.environ)
    env['NEU_BOX_WORKER_URL'] = worker_url
    # 别让外面的 NEU_BOX_CONFIG 漏进来指到别的配置文件；环境变量本来就覆盖文件，
    # 这里只是让"读哪个文件"这件事在测试里是确定的。
    env.pop('NEU_BOX_CONFIG', None)
    # 代理变量同理：hook 是被 dockerd 拉起来的，不该用开发机 shell 里的代理。
    # （不清理的话，「Worker 连不上」那条会被一个本地代理挡下来，报出来的错
    # 就不是拒连而是代理的 503 —— 测试要验的是连不上，不是代理配没配。）
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'):
        env.pop(name, None)
        env.pop(name.lower(), None)
    payload = json.dumps(state) if stdin_text is None else stdin_text
    return subprocess.run(
        [hook_binary],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def _oci_state(pid, bundle, sandbox=None, annotations=True):
    """runc 从 stdin 喂进来的 OCI state（runtime-spec 的 State）。"""
    state = {
        'ociVersion': '1.0.2',
        'id': CONTAINER_ID,
        'pid': pid,
        'bundle': str(bundle),
    }
    if annotations:
        state['annotations'] = {'sandbox_cgroup': sandbox}
    return state


def _write_bundle(directory, sandbox_name):
    """写一份 OCI bundle：``config.json`` 里带 annotation（hook 的回退来源）。

    ``sandbox_name`` 为 None 时写一份**没有** annotation 的 config.json —— 用来
    造"state 和 bundle 两边都没有"的场景。
    """
    directory.mkdir(parents=True, exist_ok=True)
    config = {'ociVersion': '1.0.2'}
    if sandbox_name is not None:
        config['annotations'] = {'sandbox_cgroup': sandbox_name}
    (directory / 'config.json').write_text(
        json.dumps(config), encoding='utf-8')
    return directory


@pytest.fixture
def bundle(tmp_path):
    """bundle 里放一个**谁都不是**的沙盒名。

    契约规定 ``sandbox_cgroup`` 优先从 state 的 annotations 取，取不到才回退到
    bundle 的 config.json。这里放个不存在的名字，precedence 反了的话正常用例
    会以 404 失败，而不是静默通过。
    """
    return _write_bundle(tmp_path / 'bundle', PLACEHOLDER_SANDBOX)


@pytest.fixture
def container_init_pid():
    """一个真的活着的进程，扮演"容器的 init"。

    Worker 侧要真的 ``os.open('/proc/<pid>/ns/mnt')``，所以 PID 必须真活着；
    它的 mnt ns inum / cgroup / starttime 都是真的。
    """
    process = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(600)'])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait(timeout=10)


# ══════════════════════════════════════════════════════════════════
# Worker：真在监听的服务 + 真数据库
# ══════════════════════════════════════════════════════════════════

class _Worker:
    """一个真在监听的 Worker，附带测试侧的观察记录。"""

    def __init__(self, db):
        self._server = None
        self.application = None
        self.db = db
        self.manager = None
        self.requests = []   # 收到的请求（请求体是 hook 真发的那份）
        self.answers = []    # 回出去的响应
        self.bindings = []   # 走到"写 BPF map"这一步的 (沙盒名, mnt ns)

    @property
    def url(self):
        return f'http://127.0.0.1:{self._server.server_port}'


def _identity_of_a_live_process(container_id, init_host_pid):
    """``runtime_container_identity`` 的替身：真读 /proc，只让开宿主 mnt ns 那一条。

    替身和原函数的差别只有"与宿主共用 mount namespace 就拒"这个分支 ——
    开发机上被测的那个 PID 本来就和宿主同名，这一条必然触发（而且是**正确**
    的拒绝）。身份的三个值仍然全部来自真的 ``/proc``。
    """
    pid = int(init_host_pid)
    if pid <= 0 or not os.path.exists(f'/proc/{pid}'):
        raise api_containers.DockerExecutorError(
            f'容器 PID {pid} 不存在', 'docker_container_pid_invalid')
    return ContainerIdentity(
        container_ref=str(container_id),
        container_id=str(container_id),
        init_host_pid=pid,
        init_start_time=process_start_time(pid),
        mount_namespace=mount_namespace_of(pid),
        container_cgroup=read_unified_cgroup(pid),
        started_at='',
    )


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """真的 Worker app + 真的 SQLite，监听在随机端口上。"""
    db_path = tmp_path / 'neu_box.db'
    migrate_database(
        db_path, MIGRATIONS_PACKAGE, REQUIRED_COLUMNS, REQUIRED_INDEXES)
    db = Database(str(db_path))
    monkeypatch.setattr(Database, '_instance', db)

    application = create_app()

    observed = _Worker(db)
    observed.application = application

    @application.before_request
    def _record_request():
        observed.requests.append({
            'path': flask.request.path,
            'body': flask.request.get_json(silent=True),
        })

    @application.after_request
    def _record_answer(response):
        observed.answers.append(
            (flask.request.path, response.status_code,
             response.get_json(silent=True)))
        return response

    monkeypatch.setattr(
        api_containers, 'runtime_container_identity',
        _identity_of_a_live_process)

    # 真的 SbxManager 方法（含 register_runtime_container 那个事务），
    # 绕开 __init__ —— 它要跑 reaper 恢复和 native 对账，都需要 root + BPF。
    manager = SbxManager.__new__(SbxManager)
    manager.db = db
    manager.lock = threading.RLock()
    manager._container_registration_lock = threading.RLock()
    manager._container_fds = {}
    manager._epoll = select.epoll()

    # 「容器退出」的占位：写端一直握着，epoll 上就永远不会有事件。
    pin_write_ends = []

    def _open_container_handles(init_host_pid):
        """真开 mnt ns fd（inum 因此是真的），pidfd 换成占位管道。"""
        mnt_fd = os.open(f'/proc/{int(init_host_pid)}/ns/mnt', os.O_RDONLY)
        read_end, write_end = os.pipe()
        pin_write_ends.append(write_end)
        return mnt_fd, os.fstat(mnt_fd).st_ino, read_end

    manager.open_container_handles = _open_container_handles
    manager.bind_container = lambda sandbox_name, mount_namespace: (
        observed.bindings.append((sandbox_name, int(mount_namespace))))
    manager.unbind_container = lambda mount_namespace: None
    monkeypatch.setattr(SbxManager, '_instance', manager)
    observed.manager = manager

    server = make_server('127.0.0.1', 0, application)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    observed._server = server
    try:
        yield observed
    finally:
        server.shutdown()
        thread.join(timeout=10)
        for mnt_fd, pid_fd in manager._container_fds.values():
            os.close(pid_fd)
            os.close(mnt_fd)
        for write_end in pin_write_ends:
            os.close(write_end)
        manager._epoll.close()


# ══════════════════════════════════════════════════════════════════
# 假 Worker：想返什么就返什么，想挂住就挂住
# ══════════════════════════════════════════════════════════════════

class _FakeWorkerHandler(BaseHTTPRequestHandler):
    """接一个 POST，记下来，然后按主人的意思答（或者不答）。"""

    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        worker = self.server.worker
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        # 原样记下来：hook 万一发了坏 JSON，解析会失败，"收到的字节"才是证据。
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        worker.requests.append({
            'path': self.path,
            'content_type': self.headers.get('Content-Type'),
            'raw_body': raw.decode('utf-8', 'replace'),
            'body': parsed,
        })

        if worker.hang:
            # 收下请求，然后不响应：真 Worker 卡在数据库锁上就是这个样子。
            # 等测试放行（最多 30s），别在 pytest 进程里留一个睡着的线程。
            worker.release.wait(timeout=30)
            # 这是"卡住"那一场，连接上不会再有别的东西了。
            self.close_connection = True
            return

        payload = json.dumps(worker.answer).encode('utf-8')
        self.send_response(worker.status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        """默认实现会往 stderr 喷日志，压掉（要看的日志是 hook 的）。"""


class _FakeWorkerServer(ThreadingHTTPServer):
    daemon_threads = True


class _FakeWorker:
    """一个可控的假 Worker：真在监听，行为由测试指定。

    它**不加载 Worker 代码**，所以验出来的任何失败都只能记在 hook 头上。
    """

    def __init__(self, status=200, answer=None, hang=False):
        self.status = status
        self.answer = {'status': 'registered'} if answer is None else answer
        self.hang = hang
        self.requests = []
        self.release = threading.Event()
        self._server = _FakeWorkerServer(('127.0.0.1', 0), _FakeWorkerHandler)
        self._server.worker = self
        self._thread = None

    @property
    def url(self):
        return f'http://127.0.0.1:{self._server.server_port}'

    def start(self):
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)


@pytest.fixture
def fake_worker():
    """假 Worker 的工厂：一次用例里起几个都行，退出时全部关掉。"""
    started = []

    def make(*, status=200, answer=None, hang=False):
        worker = _FakeWorker(status=status, answer=answer, hang=hang).start()
        started.append(worker)
        return worker

    try:
        yield make
    finally:
        for worker in started:
            worker.stop()


def _only_request(worker):
    """断言假 Worker 只收到**一个**请求，并把它返回。

    "只发一次"是契约的一部分：hook 的整个预算是 10s，重试会把超时吃光，
    而且沙盒不存在 / 身份冲突这类失败重来一次结果也不会变。
    """
    assert len(worker.requests) == 1, worker.requests
    return worker.requests[0]


def _assert_contract_body(request, *, pid, sandbox=SANDBOX):
    """hook 发出来的请求体必须**精确等于**契约里那三个必填字段。

    多一个字段就多一个把容器卡死的理由，少一个就登记不上。
    """
    assert request['path'] == '/container/register'
    assert request['content_type'] == 'application/json'
    assert request['body'] == {
        'container_id': CONTAINER_ID,
        'host_pid': pid,
        'sandbox_cgroup': sandbox,
    }


# ══════════════════════════════════════════════════════════════════
# 用例
# ══════════════════════════════════════════════════════════════════

def test_hook_registers_into_an_existing_sandbox(
        worker, hook_binary, container_init_pid, bundle):
    """沙盒存在：hook 退 0，且登记真的落进数据库。"""
    worker.db.insert_sandbox(SANDBOX, cpu=2, mem='4g')

    result = _run_hook(
        hook_binary, worker.url,
        _oci_state(container_init_pid, bundle, SANDBOX))

    assert result.returncode == 0, result.stderr
    assert result.stdout == '', 'hook 的 stdout 归 runc，日志只能走 stderr'

    # 别看状态码就完事 —— 认的是库里那一行。
    rows = worker.db.list_containers(sandbox_name=SANDBOX)
    assert len(rows) == 1, worker.answers
    row = rows[0]
    assert row['container_id'] == CONTAINER_ID
    assert row['sandbox_name'] == SANDBOX
    assert row['init_host_pid'] == container_init_pid
    assert row['init_start_time'] == process_start_time(container_init_pid)
    assert row['mount_namespace'] == mount_namespace_of(container_init_pid)

    # 登记即 join：CREATING 的沙盒被这次登记推成 ACTIVE。
    assert worker.db.get_sandbox(SANDBOX)['state'] == 'ACTIVE'
    # 走到了写授权表那一步，用的是 annotation 里那个沙盒名。
    assert worker.bindings == [(SANDBOX, row['mount_namespace'])]

    # hook 真发出来的请求体：annotation 的键和值原样落到契约字段上。
    assert worker.requests[-1]['path'] == '/container/register'
    body = worker.requests[-1]['body']
    assert body['container_id'] == CONTAINER_ID
    assert body['host_pid'] == container_init_pid
    assert body['sandbox_cgroup'] == SANDBOX
    assert set(body) <= CONTRACT_FIELDS, sorted(set(body) - CONTRACT_FIELDS)

    path, status, answer = worker.answers[-1]
    assert (path, status) == ('/container/register', 201)
    assert answer['status'] == 'registered'
    assert answer['sandbox_name'] == SANDBOX
    assert answer['mount_namespace'] == row['mount_namespace']


def test_hook_is_idempotent_when_runc_retries(
        worker, hook_binary, container_init_pid, bundle):
    """同一个容器登记两次：hook 仍然退 0，库里只有一行。"""
    worker.db.insert_sandbox(SANDBOX)
    state = _oci_state(container_init_pid, bundle, SANDBOX)

    first = _run_hook(hook_binary, worker.url, state)
    second = _run_hook(hook_binary, worker.url, state)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert len(worker.db.list_containers()) == 1
    assert [status for _path, status, _body in worker.answers] == [201, 200]


def test_hook_fails_closed_when_the_sandbox_is_unknown(
        worker, hook_binary, container_init_pid, bundle):
    """annotation 指一个不存在的沙盒名：容器必须起不来（fail-closed）。"""
    worker.db.insert_sandbox(SANDBOX)

    result = _run_hook(
        hook_binary, worker.url,
        _oci_state(container_init_pid, bundle, UNKNOWN_SANDBOX))

    assert result.returncode != 0, result.stderr
    assert result.stdout == ''
    assert result.stderr != '', 'fail-closed 必须留下原因'

    assert worker.db.list_containers() == []
    assert worker.bindings == []
    path, status, answer = worker.answers[-1]
    assert (path, status) == ('/container/register', 404)
    assert answer['code'] == 'sandbox_not_found'


def test_hook_fails_closed_when_the_worker_is_unreachable(
        worker, hook_binary, container_init_pid, bundle):
    """Worker 连不上：容器必须起不来，绝不能"登记不上就放行"。"""
    worker.db.insert_sandbox(SANDBOX)

    # 绑定再关掉：这个端口上确定没人听（连上去立刻 ECONNREFUSED）。
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    result = _run_hook(
        hook_binary, f'http://127.0.0.1:{dead_port}',
        _oci_state(container_init_pid, bundle, SANDBOX))

    assert result.returncode != 0
    assert result.stdout == ''
    assert result.stderr != ''
    # 请求根本没到 Worker —— 拒绝的原因只有"连不上"，不是 Worker 判的。
    assert worker.requests == []
    assert worker.db.list_containers() == []


def test_hook_reads_the_annotation_out_of_the_bundle(
        worker, hook_binary, container_init_pid, tmp_path):
    """state 里没有 annotations 时回退读 ``<bundle>/config.json``。

    两条来源必须产出同一份契约请求体 —— 这是 hook 自己的回退路径，
    但登记结果要和 state 那条路一模一样。
    """
    worker.db.insert_sandbox(SANDBOX)
    bundle = _write_bundle(tmp_path / 'fallback-bundle', SANDBOX)

    result = _run_hook(
        hook_binary, worker.url,
        _oci_state(container_init_pid, bundle, annotations=False))

    assert result.returncode == 0, result.stderr
    assert worker.requests[-1]['body']['sandbox_cgroup'] == SANDBOX
    rows = worker.db.list_containers(sandbox_name=SANDBOX)
    assert [row['container_id'] for row in rows] == [CONTAINER_ID]
    assert worker.answers[-1][1] == 201


def test_registration_rejection_is_not_ascii_escaped(worker):
    """中文 error 文本必须原样出现在响应体里，不能是 ``\\uXXXX``。

    这个响应会被 runtime hook 原样带进 stderr、也就是 runc 的日志。Flask 默认
    ``ensure_ascii=True`` 会把它转义成 ``\\u5fc5\\u586b...``，出错时排障的人看到
    的就是转义串（``create_app()`` 关掉了它）。
    """
    with worker.application.test_client() as client:
        response = client.post('/container/register', json={})

    assert response.status_code == 400
    assert 'container_id 和 sandbox_cgroup 为必填参数'.encode('utf-8') in response.data
    assert b'\\u5fc5' not in response.data


# ── 假 Worker：hook 的失败路径 ────────────────────────────────────────

@pytest.mark.parametrize('status, status_text, code', [
    # Worker 自己炸了（数据库、锁、没接住的异常）。
    (500, '500 Internal Server Error', None),
    # 请求本身有问题。契约里 400 没有 code —— 能说的只有 HTTP 状态和 error 文本。
    (400, '400 Bad Request', None),
    # 沙盒正在销毁：真 Worker 会带上 code，hook 得把它带出来。
    (409, '409 Conflict', 'sandbox_not_active'),
])
def test_hook_fails_closed_when_the_worker_says_no(
        fake_worker, hook_binary, container_init_pid, bundle,
        status, status_text, code):
    """Worker 明确拒绝（500 / 400 / 409）：容器必须起不来，且只发一次。

    hook 认的是"2xx 才是登记上了"，任何非 2xx 都退非零 —— 这条不能靠"Worker
    只会在真出问题时才返 4xx/5xx"来兜底，所以这里主动造。
    """
    # 原因文本里特意不放状态码：下面那处断言只可能由状态行（Go 的
    # ``response.Status``，也就是 500 后面跟的那串英文）满足，端口号冒充不了。
    #
    # 这里是英文的：对面是**假 Worker**（手写 ``http.server``，不加载 Worker
    # 代码），用它自己的编码器回话。换成中文，验的就掺进了"响应体用什么编码"
    # 这件事，而这条用例要验的是"hook 把 Worker 给的原因带出来了"。真 Worker
    # 那边 ``create_app()`` 已经关掉 ``ensure_ascii``，中文原样进响应体。
    detail = {'error': 'fake worker: this sandbox is not available'}
    if code:
        detail['code'] = code
    server = fake_worker(status=status, answer=detail)

    result = _run_hook(
        hook_binary, server.url,
        _oci_state(container_init_pid, bundle, SANDBOX))

    assert result.returncode != 0, '非 2xx 必须退非零，绝不能放行'
    assert result.stdout == '', 'hook 的 stdout 归 runc，日志只能走 stderr'
    # 状态行和 Worker 给的原因都要进 stderr：runc 只留 stderr，说不清是什么错
    # 就没法查。409 的 code 是排障的第一手信息。
    assert status_text in result.stderr, result.stderr
    assert detail['error'] in result.stderr, result.stderr
    if code:
        assert code in result.stderr, result.stderr

    # 只发一次：失败是确定性的，重试只会把 10s 预算吃光。
    _assert_contract_body(_only_request(server), pid=container_init_pid)


def test_hook_times_out_before_runc_can_kill_it(
        fake_worker, hook_binary, container_init_pid, bundle):
    """Worker 挂住不响应：hook 必须在自己的 10s 预算内退非零。

    这条验的是一个**数量关系**，不是"能不能连上"：hook 的 HTTP 超时
    （``httpTimeout``，8s）必须严格小于它交给 runc 的 timeout（10s）。反过来的话
    超时那次就不是 hook 自己退非零，而是被 runc 杀掉 —— 那时 stderr 里一个字都
    没有，容器起不来的原因无从查起，而这正是"绝不放行"最不想要的结果。
    """
    server = fake_worker(hang=True)

    started = time.monotonic()
    result = _run_hook(
        hook_binary, server.url,
        _oci_state(container_init_pid, bundle, SANDBOX))
    elapsed = time.monotonic() - started

    assert result.returncode != 0, '等不到响应也必须退非零'
    assert result.stdout == ''
    # 卡住的那次请求真的发出去了 —— 否则验的就成了"连不上"。
    _assert_contract_body(_only_request(server), pid=container_init_pid)
    # 是 HTTP 超时自己退的，不是别的什么把它弄死的。
    assert 'Timeout' in result.stderr or 'deadline' in result.stderr, result.stderr
    # 8s 是 HTTP 超时，10s 是 hook 给 runc 的 timeout（另一侧写进 OCI hook 记录）。
    assert elapsed >= 7.9, f'HTTP 超时不到 8s 就退了（{elapsed:.2f}s），说明验的不是超时'
    assert elapsed < 10, (
        f'HTTP 超时没有严格小于 hook 的 10s 预算（实际 {elapsed:.2f}s）：'
        '这样超时那次会是被 runc 杀掉，stderr 里什么都不会留下')


def test_hook_rejects_a_broken_oci_state_without_hanging(
        fake_worker, hook_binary):
    """stdin 是坏 JSON：立刻退非零，不挂死，也不去骚扰 Worker。

    runc 的管道写完就关，但"读 stdin"这条路径一旦写成等 EOF 之类的样子，
    容器创建就会挂在这里 —— 而 runc 那边看到的是 hook 不返回，不是 hook 报错。
    """
    server = fake_worker(status=201, answer={'status': 'registered'})

    started = time.monotonic()
    result = _run_hook(
        hook_binary, server.url, None, stdin_text='{')
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert result.stdout == ''
    assert result.stderr != '', '退非零就得留下原因'
    assert elapsed < 5, f'读不出 state 就该立刻退，不该在 stdin 上等（{elapsed:.2f}s）'
    assert server.requests == [], 'state 都没读出来，一个请求都不该发'


def test_hook_rejects_a_state_with_no_annotation_anywhere(
        fake_worker, hook_binary, container_init_pid, tmp_path):
    """state 和 bundle 两处都没有 annotation：退非零，且一个请求都不发。

    和"沙盒不存在"（Worker 判的 404）不是一回事：这里是 hook 自己发现
    **根本没有沙盒名可报**。不能猜、不能取默认沙盒 —— 猜错就是把容器登记到
    别人的沙盒名下。
    """
    server = fake_worker()

    # ① bundle 在，但 config.json 里也没有 annotation。
    bare_bundle = _write_bundle(tmp_path / 'bare-bundle', None)
    without_annotation = _run_hook(
        hook_binary, server.url,
        _oci_state(container_init_pid, bare_bundle, annotations=False))

    # ② 连 bundle 路径都没有，没得回退。
    without_bundle = _run_hook(
        hook_binary, server.url,
        _oci_state(container_init_pid, '', annotations=False))

    for result in (without_annotation, without_bundle):
        assert result.returncode != 0
        assert result.stdout == ''
        assert result.stderr != ''
        # 原因要说清是 annotation 没了 —— 报的是契约里那个键名。
        assert 'sandbox_cgroup' in result.stderr, result.stderr
    assert server.requests == [], server.requests
