"""docker 执行后端: 短命令的退出路径，以及 runtime 登记复核。

两个都要钉住的相反面:

  - 容器跑得比执行器看它更快时（``echo`` 这类），**不能**报失败 —— 身份随
    ``/proc`` 一起消失、登记记录又被 pidfd 收尸线程删掉，两个观测点都被合法
    地清干净了，据此报错就是拿"看不见"当"不存在"。
  - 容器还活着时，登记复核照旧 fail-closed —— 身份读得出来就说明记录不可能
    已经被收走，此时"查不到"只能是"从没登记过"。

两条针对的是同一次改动的两侧，缺一条都会让"把复核整个删掉"这种改法看起来
是对的。
"""

import pytest

from neu_box.execution.docker import (
    DockerCommandExecutor,
    _REGISTRATION_TIMEOUT,
)
from neu_box.runtime.containers import ContainerIdentity, DockerExecutorError

SANDBOX = 'sbx_yuxd_task-1.slice'
CONTAINER_ID = 'container-id'
MNT_NS = 4026533000


# ── docker 替身 ─────────────────────────────────────────────────


class _Container:
    def __init__(self, state):
        self.attrs = {'State': dict(state)}
        self.logs_called = False
        self.removed = False

    def reload(self):
        pass

    def logs(self, **_options):
        self.logs_called = True
        return []

    def kill(self):
        pass

    def remove(self, force=False):
        self.removed = True


class _Containers:
    def __init__(self, container):
        self._container = container

    def get(self, _ref):
        return self._container


class _API:
    def create_host_config(self, **_options):
        return {}

    def create_container(self, _image, **_options):
        return {'Id': CONTAINER_ID}

    def start(self, _container_id):
        pass


class _Client:
    """只够 ``_run_blocking`` 跑一遍的最小 client。

    ``state`` 就是容器"现在"的状态: ``running=False`` 模拟短命令在
    ``_wait_identity`` 首轮之前就已经退出。
    """

    def __init__(self, running=False, exit_code=0):
        self.state = {'Running': running, 'ExitCode': exit_code, 'Pid': 4242}
        self.container = _Container(self.state)
        self.api = _API()
        self.containers = _Containers(self.container)
        self.closed = False

    def close(self):
        self.closed = True


class _DB:
    def __init__(self, records=None):
        self.records = dict(records or {})

    def get_container(self, mount_namespace):
        return self.records.get(int(mount_namespace))


class _Manager:
    def __init__(self, records=None):
        self.db = _DB(records)
        self.released = []

    def release_container(self, mount_namespace):
        self.released.append(int(mount_namespace))


def _identity():
    return ContainerIdentity(
        container_ref=CONTAINER_ID,
        container_id=CONTAINER_ID,
        init_host_pid=4242,
        init_start_time=99,
        mount_namespace=MNT_NS,
        container_cgroup='/docker/container-id',
        started_at='',
    )


def _executor(monkeypatch, tmp_path):
    manager = _Manager()
    monkeypatch.setattr('neu_box.execution.docker.devices.node_paths', list)
    monkeypatch.setattr(
        'neu_box.execution.docker.SbxManager.get_instance',
        classmethod(lambda cls: manager),
    )
    executor = DockerCommandExecutor(
        task={'target_spec': {'image': 'image:tag'}, 'command': 'echo ok'},
        sandbox_name=SANDBOX, devices=[], log_path=str(tmp_path / 'task.log'),
    )
    return executor, manager


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        'neu_box.execution.docker.docker_client', lambda timeout=None: client,
    )
    return client


def _exits_after_first_probe(monkeypatch, executor, client, exit_code=0):
    """第一次 ``_state`` 报 running，之后报退出。

    收尾（``_ensure_container_stopped``）也是读 ``_state`` —— 不换成"已退出"
    的话它会老老实实杀容器再等满 5 秒超时，把测试拖慢。
    """
    calls = []

    def _state(_client):
        calls.append(1)
        if len(calls) == 1:
            return client.state
        return {'Running': False, 'ExitCode': exit_code}

    monkeypatch.setattr(executor, '_state', _state)


def _live_identity(monkeypatch):
    """假装容器还活着、身份读得出来。"""
    monkeypatch.setattr(
        'neu_box.execution.docker.container_identity',
        lambda _ref, timeout=None: _identity(),
    )


def _exited_identity(monkeypatch):
    """假装容器已经不在跑 —— ``container_identity`` 对已退出容器的真实反应。

    ``container_identity`` 是唯一会去连 Docker 的函数，这里换成它的实际
    行为：状态不是 running 就抛 ``docker_container_not_running``。测试因此
    不碰真 daemon。
    """
    def _stopped(_ref, timeout=None):
        raise DockerExecutorError(
            f'容器 {CONTAINER_ID} 必须处于 running 且未 paused',
            'docker_container_not_running',
        )

    monkeypatch.setattr(
        'neu_box.execution.docker.container_identity', _stopped,
    )


# ── 短命令: 容器先退出 ───────────────────────────────────────────


def test_short_command_that_already_exited_is_not_reported_as_failure(
        monkeypatch, tmp_path):
    """容器退 0、且退在我们看清它之前 → 任务必须报成功。

    变异: 把 ``_wait_identity`` 里 ``docker_container_not_running`` 那一支
    去掉（恢复成直接 raise），或者在 ``identity is None`` 那一支里报失败，
    这条都会红。
    """
    executor, manager = _executor(monkeypatch, tmp_path)
    client = _patch_client(monkeypatch, _Client(running=False, exit_code=0))
    _exited_identity(monkeypatch)

    outcome = executor._run_blocking(timeout=None)

    assert outcome['error'] is None, outcome
    assert outcome['returncode'] == 0, outcome
    assert outcome['timed_out'] is False
    # 输出还是要收: 退出之后 docker 仍能给出缓冲日志。
    assert client.container.logs_called
    assert client.container.removed, '容器仍要被收掉'


def test_short_command_records_the_real_exit_code(monkeypatch, tmp_path):
    """失败的命令照样要把真实退出码带出来，不能变成 -1。"""
    executor, _manager = _executor(monkeypatch, tmp_path)
    _patch_client(monkeypatch, _Client(running=False, exit_code=17))
    _exited_identity(monkeypatch)

    outcome = executor._run_blocking(timeout=None)

    assert outcome['returncode'] == 17, outcome
    assert outcome['error'] is None, outcome


def test_exited_container_skips_the_registration_probe(monkeypatch, tmp_path):
    """容器已退出时**不**去问 DB —— 记录可能已被收尸线程删掉。

    变异: 保留一个"退而求其次按 container_id 查"的调用，或在 ``None`` 分支
    里读 ``db.get_container``，这条会红。
    """
    executor, manager = _executor(monkeypatch, tmp_path)
    _patch_client(monkeypatch, _Client(running=False))
    _exited_identity(monkeypatch)

    def _forbidden(_mount_namespace):
        raise AssertionError('容器已退出时不应再去查登记记录')

    manager.db.get_container = _forbidden

    outcome = executor._run_blocking(timeout=None)

    assert outcome['error'] is None, outcome
    assert (
        'runtime 登记未复核'
        in open(executor.log.path, encoding='utf-8').read()
    ), '跳过复核这件事要留在任务日志里'
    assert manager.released == [], '没有身份可采纳，注销交给对账'


# ── 还活着的容器: 复核照旧 fail-closed ───────────────────────────


def test_live_container_without_a_registration_still_fails(monkeypatch, tmp_path):
    """容器还在跑、但没有登记记录 → 仍然是失败。

    变异: 把 ``_verify_registration`` 整个删掉，这条会红。它挡的是"runtime
    没配好、hook 压根没跑"——身份读得出来时这个判断是可靠的。
    """
    executor, _manager = _executor(monkeypatch, tmp_path)
    client = _patch_client(monkeypatch, _Client(running=True))
    _exits_after_first_probe(monkeypatch, executor, client)
    _live_identity(monkeypatch)

    outcome = executor._run_blocking(timeout=None)

    assert outcome['returncode'] == -1, outcome
    assert '未在 runtime 启动钩子中登记' in outcome['error'], outcome


def test_live_container_registered_to_another_sandbox_fails(monkeypatch, tmp_path):
    """记录存在但归属别的沙盒 → 失败，不能只判"记录存在"。"""
    executor, manager = _executor(monkeypatch, tmp_path)
    manager.db.records[MNT_NS] = {'sandbox_name': 'sbx_someone_else.slice'}
    client = _patch_client(monkeypatch, _Client(running=True))
    _exits_after_first_probe(monkeypatch, executor, client)
    _live_identity(monkeypatch)

    outcome = executor._run_blocking(timeout=None)

    assert outcome['returncode'] == -1, outcome
    assert '未在 runtime 启动钩子中登记' in outcome['error'], outcome


def test_registered_live_container_runs_to_completion(monkeypatch, tmp_path):
    """正常路径: 登记在册 → 采纳身份（收尾要用）→ 照常等退出。"""
    executor, manager = _executor(monkeypatch, tmp_path)
    manager.db.records[MNT_NS] = {'sandbox_name': SANDBOX}
    client = _patch_client(monkeypatch, _Client(running=True))
    _exits_after_first_probe(monkeypatch, executor, client, exit_code=3)
    _live_identity(monkeypatch)

    outcome = executor._run_blocking(timeout=None)

    assert outcome['returncode'] == 3, outcome
    assert outcome['error'] is None, outcome
    # 身份被记下来，收尾时才能按 init PID 收容器 / 注销登记。
    assert executor._init_host_pid == 4242
    assert manager.released == [MNT_NS]


def test_init_pid_not_yet_visible_is_waited_for(monkeypatch, tmp_path):
    """``docker_container_pid_invalid`` 照旧重试，不当成"已经退出"。"""
    executor, _manager = _executor(monkeypatch, tmp_path)
    _patch_client(monkeypatch, _Client(running=True))
    monkeypatch.setattr(
        'neu_box.execution.docker._POLL_INTERVAL', 0.0)
    attempts = []

    def _identity_late(_ref, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise DockerExecutorError('init 还没落', 'docker_container_pid_invalid')
        return _identity()

    monkeypatch.setattr(
        'neu_box.execution.docker.container_identity', _identity_late)

    identity = executor._wait_identity(None)

    assert identity is not None
    assert identity.mount_namespace == MNT_NS
    assert len(attempts) == 3


def test_unknown_identity_errors_are_not_swallowed(monkeypatch, tmp_path):
    """既不重试也不当"已退出"：别的身份错误照旧往上抛。"""
    executor, _manager = _executor(monkeypatch, tmp_path)

    def _broken(_ref, timeout=None):
        raise DockerExecutorError('跟宿主机共用 mnt ns', 'docker_container_same_mount_namespace')

    monkeypatch.setattr('neu_box.execution.docker.container_identity', _broken)

    with pytest.raises(DockerExecutorError) as excinfo:
        executor._wait_identity(None)

    assert excinfo.value.code == 'docker_container_same_mount_namespace'


def test_identity_wait_gives_up_at_the_deadline(monkeypatch, tmp_path):
    """一直读不到身份（不是"已退出"）时仍然是超时失败。"""
    executor, _manager = _executor(monkeypatch, tmp_path)
    monkeypatch.setattr('neu_box.execution.docker._REGISTRATION_TIMEOUT', 0.0)
    monkeypatch.setattr(
        'neu_box.execution.docker.container_identity',
        lambda _ref, timeout=None: (_ for _ in ()).throw(
            DockerExecutorError('还没就绪', 'docker_container_pid_invalid')),
    )

    with pytest.raises(DockerExecutorError) as excinfo:
        executor._wait_identity(None)

    assert excinfo.value.code == 'docker_container_not_ready'
    assert _REGISTRATION_TIMEOUT > 0, '真超时不能被改成 0'
