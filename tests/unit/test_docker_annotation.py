"""Worker 建任务容器时必须按契约传 ``sandbox_cgroup`` annotation。

键、值、以及"塞在 ``HostConfig.Annotations`` 上"这个反直觉的写法都是跨仓库
契约（``docs/container-registration.md``），而 docker-py 没有一等参数 —— 所以用测试钉住，
免得下一个人把这个将就写法当成 bug 改掉。
"""

from neu_box.execution.docker import DockerCommandExecutor
from neu_box.runtime import cgroup

SANDBOX = 'sbx_yuxd_task-1.slice'


class _API:
    def __init__(self):
        self.host_options = None
        self.options = None
        self.created = None
        self.started = None

    def create_host_config(self, **options):
        self.host_options = options
        return {}

    def create_container(self, image, **options):
        self.created = image
        self.options = options
        return {'Id': 'container-id'}

    def start(self, container_id):
        self.started = container_id


class _Client:
    def __init__(self):
        self.api = _API()


def _executor(monkeypatch, tmp_path, target=None, task=None):
    monkeypatch.setattr('neu_box.execution.docker.devices.node_paths', list)
    monkeypatch.setattr(
        'neu_box.execution.docker.SbxManager.get_instance',
        classmethod(lambda cls: object()),
    )
    task = dict(
        task
        or {'target_spec': {'image': 'image:tag', 'env': {'USER_VAR': 'ok'}},
            'command': 'echo ok'}
    )
    if target is not None:
        task['target_spec'] = target
    return DockerCommandExecutor(
        task=task, sandbox_name=SANDBOX, devices=[],
        log_path=str(tmp_path / 'task.log'),
    )


def test_start_container_annotates_the_sandbox_name(monkeypatch, tmp_path):
    executor = _executor(monkeypatch, tmp_path)
    client = _Client()

    executor._start_container(client)

    host_config = client.api.options['host_config']
    assert host_config['Annotations'] == {'sandbox_cgroup': SANDBOX}
    assert client.api.started == 'container-id'
    assert client.api.options['detach'] is True
    # 用户环境变量照旧单独走 Config.Env，不掺进 annotation。
    assert client.api.options['environment'] == {'USER_VAR': 'ok'}


def test_start_container_uses_no_high_level_run_helper(monkeypatch, tmp_path):
    """annotation 没有注入口：必须走 create_host_config + create_container。"""
    executor = _executor(monkeypatch, tmp_path)
    client = _Client()
    monkeypatch.setattr(
        client.api, 'create_host_config', client.api.create_host_config)

    executor._start_container(client)

    assert client.api.host_options is not None, 'host_config 必须由自己构造'
    assert client.api.created == 'image:tag'


def test_start_container_ignores_sandbox_named_by_the_target(monkeypatch, tmp_path):
    """沙盒归属由 Worker 分配，不接受请求方指定。"""
    executor = _executor(
        monkeypatch, tmp_path,
        target={'image': 'image:tag', 'sandbox_cgroup': 'sbx_evil_task-9.slice'},
    )
    client = _Client()

    executor._start_container(client)

    assert client.api.options['host_config']['Annotations'] == {
        'sandbox_cgroup': SANDBOX,
    }


def test_start_container_labels_are_not_an_annotation_channel(monkeypatch, tmp_path):
    """labels 留在 Docker 侧，用于按 label 扫残留容器，不进 OCI bundle。"""
    executor = _executor(monkeypatch, tmp_path)
    client = _Client()

    executor._start_container(client)

    assert client.api.options['labels'] == {
        'neu-box.sandbox': SANDBOX,
        'neu-box.sandbox-cgroup': cgroup.path(SANDBOX),
    }
    assert 'sandbox_cgroup' not in client.api.options['labels']
