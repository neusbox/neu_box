"""Issue #9：已有 Docker 容器交互终端加入 sandbox。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from flask import Flask

from neu_box.worker.executor import (
    command_docker,
    container_terminal,
    sandbox_api,
)
from neu_box.worker.executor.command_docker import ContainerProcess


def _process(container_pid: int, host_pid: int) -> ContainerProcess:
    return ContainerProcess(
        container_ref='oprace',
        container_id='container-id',
        container_started_at='2026-08-13T00:00:00Z',
        container_pid=container_pid,
        host_pid=host_pid,
        host_start_time=1000 + host_pid,
        init_host_pid=9000,
        init_start_time=999,
        container_cgroup='/system.slice/docker-oprace.scope',
    )


class _ScanEntries:
    def __init__(self, names):
        self._entries = [SimpleNamespace(name=str(name)) for name in names]

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *_args):
        return False


class _FakeContainer:
    id = 'container-id'

    def __init__(self):
        self.attrs = {
            'State': {
                'Running': True,
                'Paused': False,
                'Pid': 9000,
                'StartedAt': '2026-08-13T00:00:00Z',
            }
        }

    def reload(self):
        pass


class _FakeDockerClient:
    def __init__(self):
        self.container = _FakeContainer()
        self.containers = SimpleNamespace(get=lambda _ref: self.container)
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSbx:
    def __init__(self):
        self.joined = []
        self.moved = []
        self.destroyed = []
        self.allocated = []
        self.record = {'devices': [], 'pids': [43210]}
        self.db = SimpleNamespace(get_sandbox=lambda _name: self.record)

    def allocate_sandbox(self, **kwargs):
        self.allocated.append(kwargs)
        return {
            'sandbox_name': 'sbx_yuxd_43210.slice',
            'devices': [],
        }

    def join_sandbox(self, name, pid):
        self.joined.append((name, pid))
        return True

    def move_pid_to_cgroup(self, pid, path):
        self.moved.append((pid, path))
        return True

    def destroy_sandbox(self, name):
        self.destroyed.append(name)
        return True

    def _discover_device_nodes(self, _root='/dev'):
        return []


def test_resolve_container_pid_uses_nspid_and_namespace():
    client = _FakeDockerClient()
    namespace_pids = {
        9000: (9000, 1),
        111: (111,),
        43210: (43210, 12),
        54321: (54321, 13),
    }

    def start_time(pid):
        return {9000: 999, 43210: 44210}[pid]

    with (
        mock.patch.object(command_docker, '_docker_client', return_value=client),
        mock.patch.object(
            command_docker,
            '_load_docker',
            return_value=SimpleNamespace(
                errors=SimpleNamespace(NotFound=LookupError),
            ),
        ),
        mock.patch.object(command_docker.os.path, 'exists', return_value=True),
        mock.patch.object(
            command_docker.os,
            'scandir',
            return_value=_ScanEntries(namespace_pids),
        ),
        mock.patch.object(command_docker, '_namespace_inode', return_value=77),
        mock.patch.object(
            command_docker,
            '_namespace_pids',
            side_effect=lambda pid: namespace_pids[pid],
        ),
        mock.patch.object(
            command_docker,
            '_same_container_namespaces',
            side_effect=lambda pid, _init: pid == 43210,
        ),
        mock.patch.object(
            command_docker,
            '_process_start_time',
            side_effect=start_time,
        ),
        mock.patch.object(
            command_docker,
            '_read_unified_cgroup',
            return_value='/system.slice/docker-oprace.scope',
        ),
    ):
        result = command_docker.resolve_container_pid('oprace', 12)

    assert result.host_pid == 43210
    assert result.container_pid == 12
    assert result.container_cgroup == '/system.slice/docker-oprace.scope'
    assert client.closed


def test_find_sandbox_strips_filesystem_prefix():
    content = '0::/sandbox_sbx_yuxd_43210.slice\n'
    with mock.patch('builtins.open', mock.mock_open(read_data=content)):
        assert (
            sandbox_api._find_sandbox_for_pid(43210)
            == 'sbx_yuxd_43210.slice'
        )


def test_join_container_terminal_stops_checks_and_moves():
    sbx = _FakeSbx()
    process = _process(12, 43210)
    with (
        mock.patch.object(container_terminal.os, 'kill') as kill,
        mock.patch.object(container_terminal, '_wait_stopped'),
        mock.patch.object(container_terminal, 'verify_container_process'),
        mock.patch.object(
            container_terminal,
            '_read_unified_cgroup',
            side_effect=[
                '/system.slice/docker-oprace.scope',
                '/sandbox_sbx_yuxd_43210.slice',
            ],
        ),
    ):
        container_terminal.join_container_terminal(
            sbx, 'sbx_yuxd_43210.slice', process,
        )

    assert sbx.joined == [('sbx_yuxd_43210.slice', 43210)]
    assert kill.call_count == 2


def test_restore_container_terminal_moves_shell_and_http_client():
    sbx = _FakeSbx()
    shell = _process(12, 43210)
    client = _process(34, 43211)
    sandbox = '/sandbox_sbx_yuxd_43210.slice'
    with (
        mock.patch.object(container_terminal.os, 'kill'),
        mock.patch.object(container_terminal, '_wait_stopped'),
        mock.patch.object(container_terminal, 'verify_container_process'),
        mock.patch.object(
            container_terminal,
            '_read_unified_cgroup',
            side_effect=[sandbox, sandbox, sandbox, sandbox],
        ),
    ):
        container_terminal.restore_container_terminal(
            sbx, 'sbx_yuxd_43210.slice', shell, client,
        )

    assert sbx.moved == [
        (43210, '/system.slice/docker-oprace.scope'),
        (43211, '/system.slice/docker-oprace.scope'),
    ]


def _api_client():
    app = Flask(__name__)
    app.register_blueprint(sandbox_api.sandbox_bp, url_prefix='/sandbox')
    return app.test_client()


def test_acquire_maps_container_pid_before_existing_sandbox_flow():
    sbx = _FakeSbx()
    process = _process(12, 43210)
    with (
        mock.patch.object(
            sandbox_api.SbxManager,
            'get_instance',
            return_value=sbx,
        ),
        mock.patch.object(
            sandbox_api,
            'resolve_container_pid',
            return_value=process,
        ) as resolve,
        mock.patch.object(
            sandbox_api,
            '_find_sandbox_for_pid',
            return_value=None,
        ),
        mock.patch.object(
            sandbox_api,
            'join_container_terminal',
        ) as join,
    ):
        response = _api_client().post('/sandbox/acquire', json={
            'username': 'yuxd',
            'pid': 12,
            'container': 'oprace',
            'device_num': 1,
            'device_ids': None,
            'cpu': 0,
            'memory': 0,
            'mem_unit': 'GB',
        })

    assert response.status_code == 201, response.get_json()
    resolve.assert_called_once_with('oprace', 12)
    assert sbx.allocated[0]['sandbox_id'] == '43210'
    join.assert_called_once_with(
        sbx, 'sbx_yuxd_43210.slice', process,
    )


def test_acquire_keeps_sandbox_when_failed_join_cgroup_is_unknown():
    sbx = _FakeSbx()
    process = _process(12, 43210)
    error = command_docker.DockerExecutorError(
        'join failed',
        'docker_container_pid_join_failed',
    )
    with (
        mock.patch.object(
            sandbox_api.SbxManager,
            'get_instance',
            return_value=sbx,
        ),
        mock.patch.object(
            sandbox_api,
            'resolve_container_pid',
            return_value=process,
        ),
        mock.patch.object(
            sandbox_api,
            '_find_sandbox_for_pid',
            return_value=None,
        ),
        mock.patch.object(
            sandbox_api,
            'join_container_terminal',
            side_effect=error,
        ),
        mock.patch.object(
            sandbox_api,
            '_read_unified_cgroup',
            side_effect=OSError('unknown cgroup'),
        ),
    ):
        response = _api_client().post('/sandbox/acquire', json={
            'username': 'yuxd',
            'pid': 12,
            'container': 'oprace',
            'device_num': 1,
            'cpu': 0,
            'memory': 0,
        })

    assert response.status_code == 500
    assert sbx.destroyed == []


def test_release_restores_registered_terminal_before_destroy():
    sbx = _FakeSbx()
    shell = _process(12, 43210)
    client = _process(34, 43211)
    with (
        mock.patch.object(
            sandbox_api.SbxManager,
            'get_instance',
            return_value=sbx,
        ),
        mock.patch.object(
            sandbox_api,
            'resolve_container_pid',
            side_effect=[shell, client],
        ),
        mock.patch.object(
            sandbox_api,
            'restore_container_terminal',
        ) as restore,
    ):
        response = _api_client().post('/sandbox/release', json={
            'sandbox_name': 'sbx_yuxd_43210.slice',
            'container': 'oprace',
            'pid': 12,
            'client_pid': 34,
        })

    assert response.status_code == 200, response.get_json()
    restore.assert_called_once_with(
        sbx, 'sbx_yuxd_43210.slice', shell, client,
    )
    assert sbx.destroyed == ['sbx_yuxd_43210.slice']


def test_release_rejects_terminal_not_registered_in_sandbox():
    sbx = _FakeSbx()
    sbx.record = {'devices': [], 'pids': [99999]}
    with (
        mock.patch.object(
            sandbox_api.SbxManager,
            'get_instance',
            return_value=sbx,
        ),
        mock.patch.object(
            sandbox_api,
            'resolve_container_pid',
            side_effect=[_process(12, 43210), _process(34, 43211)],
        ),
        mock.patch.object(
            sandbox_api,
            'restore_container_terminal',
        ) as restore,
    ):
        response = _api_client().post('/sandbox/release', json={
            'sandbox_name': 'sbx_yuxd_43210.slice',
            'container': 'oprace',
            'pid': 12,
            'client_pid': 34,
        })

    assert response.status_code == 409
    restore.assert_not_called()
    assert sbx.destroyed == []


def test_list_reports_container_terminal_current_sandbox():
    process = _process(12, 43210)
    database = SimpleNamespace(list_sandboxes=lambda: [])
    with (
        mock.patch.object(
            sandbox_api,
            'resolve_container_pid',
            return_value=process,
        ),
        mock.patch.object(
            sandbox_api,
            '_find_sandbox_for_pid',
            return_value='sbx_yuxd_43210.slice',
        ),
        mock.patch.object(
            sandbox_api.Database,
            'get_instance',
            return_value=database,
        ),
    ):
        response = _api_client().get(
            '/sandbox/list?username=yuxd&container=oprace&pid=12',
        )

    assert response.status_code == 200
    assert response.get_json()['current_sandbox'] == 'sbx_yuxd_43210.slice'
