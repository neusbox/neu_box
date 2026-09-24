"""设备节点: "哪些是真的卡"（分配）和"容器要挂哪些节点"（--device）是两个集合。

前者是 ``scan_nodes`` / ``discover_nodes``：资源记账、major 判定、给谁哪几张
都以它为准，多一个 ``davinci_manager`` 就是多一张卡。
后者是 ``node_paths``：容器 ``--device`` 要带上驱动的管理设备，否则容器里的
第一次 NPU 初始化根本起不来。

用假 /dev（monkeypatch ``devices.os``）而不是真 ``/dev``: 真 /dev 依赖机器
装没装昇腾驱动，``mknod`` 又要 root。
"""

import os
import re
import stat

import pytest

from neu_box.runtime import devices

# 实测节点布局（昇腾 910 机器）: 卡在 234，管理设备各占一个 major。
_CARDS = {'davinci0': (234, 0), 'davinci1': (234, 1), 'davinci2': (234, 2)}
_AUXILIARY = {
    'davinci_manager': (235, 0),
    'devmm_svm': (511, 0),
    'hisi_hdc': (510, 0),
}


class _FakeStat:
    def __init__(self, mode, rdev=0):
        self.st_mode = mode
        self.st_rdev = rdev


class _FakeOS:
    """只够 ``devices.py`` 用的 ``os`` 替身（listdir / stat / major / minor）。"""

    path = os.path

    def __init__(self, entries):
        self.entries = dict(entries)

    def listdir(self, _root):
        return list(self.entries)

    def stat(self, path):
        name = path.rsplit('/', 1)[-1]
        if name not in self.entries:
            raise FileNotFoundError(path)
        return self.entries[name]

    major = staticmethod(os.major)
    minor = staticmethod(os.minor)


def _char(major, minor):
    return _FakeStat(stat.S_IFCHR | 0o660, os.makedev(major, minor))


def _fake_dev(monkeypatch, filter_pattern, extra=None, absent=()):
    entries = {name: _char(*spec) for name, spec in _CARDS.items()}
    entries.update({name: _char(*spec) for name, spec in _AUXILIARY.items()})
    entries.update(dict(extra or {}))
    for name in absent:
        entries.pop(name, None)
    monkeypatch.setattr(devices, 'os', _FakeOS(entries))
    monkeypatch.setattr(
        devices, 'env_text', lambda key: filter_pattern if key ==
        'NEU_BOX_DEVICE_FILTER' else '',
    )


# ── 分配口径: 只有卡 ─────────────────────────────────────────────


def test_scan_nodes_counts_only_the_cards(monkeypatch):
    """``scan_nodes`` 是"这台机器上有几张卡" —— 管理设备不在里面。

    变异: 把 ``davinci_manager`` 塞进 ``NEU_BOX_DEVICE_FILTER`` 的匹配集
    （或者让 ``scan_nodes`` 也认辅助节点），这条会红。
    """
    _fake_dev(monkeypatch, 'davinci[0-9]+')

    assert [path for path, _device in devices.scan_nodes()] == [
        '/dev/davinci0', '/dev/davinci1', '/dev/davinci2',
    ]


def test_discover_nodes_ignores_auxiliary_nodes(monkeypatch):
    """记账口径: 辅助节点一个都不能进。

    ``discover_nodes`` 喂的是 ``scheduling/resources.py`` 的可用卡数、
    ``SbxManager._device_major`` 的受管 major 判定和 ``/sandbox/*`` 的
    ``all_devices``。多一个 235:0 就多一张卡，而且 major 从 234 变成两个 →
    ``_device_major`` 直接抛"受管设备包含多个 major"。
    """
    _fake_dev(monkeypatch, 'davinci[0-9]+')

    managed = devices.discover_nodes()

    assert managed == ['234:0', '234:1', '234:2']
    assert {device.split(':', 1)[0] for device in managed} == {'234'}


def test_card_major_is_the_only_major_in_the_allocation_set(monkeypatch):
    """分配集合里只有一个 major —— 这是 native sandbox 能工作的前提。"""
    _fake_dev(monkeypatch, 'davinci[0-9]+')

    majors = {int(device.split(':', 1)[0]) for device in devices.discover_nodes()}

    assert majors == {234}


# ── 挂载口径: 卡 + 驱动管理设备 ─────────────────────────────────

def test_node_paths_adds_the_driver_auxiliary_nodes(monkeypatch):
    """容器 ``--device`` 必须带上 manager / hdc / svm，否则驱动初始化不了。

    变异: ``node_paths`` 退回成 ``scan_nodes`` 的纯路径投影（就是改之前的
    样子），这条会红 —— 也就是漏掉了 manager / hdc / devmm_svm。
    """
    _fake_dev(monkeypatch, 'davinci[0-9]+')

    paths = devices.node_paths()

    assert paths[:3] == ['/dev/davinci0', '/dev/davinci1', '/dev/davinci2']
    for name in devices.AUXILIARY_DEVICE_NAMES:
        assert f'/dev/{name}' in paths, f'{name} 必须挂进容器'


def test_auxiliary_nodes_cannot_come_from_the_card_filter(monkeypatch):
    """辅助节点永远不在 ``davinci[0-9]+`` 的匹配集里。

    这就是它们必须单独列的原因，也是"把它们并进 filter 就好了"这种改法
    在这里会红的原因。
    """
    _fake_dev(monkeypatch, 'davinci[0-9]+')
    regex = re.compile('davinci[0-9]+')

    for name in devices.AUXILIARY_DEVICE_NAMES:
        assert not regex.fullmatch(name)
        assert f'/dev/{name}' not in [
            path for path, _device in devices.scan_nodes()
        ]


def test_no_managed_card_means_no_device_nodes_at_all(monkeypatch):
    """一张受管卡都没有时不挂任何节点。

    没有受管 major 就没有 BPF 强制，此时把辅助节点递出去是白给设备访问权。
    """
    _fake_dev(monkeypatch, 'nvidia[0-9]+')

    assert devices.node_paths() == []


def test_node_paths_skips_missing_and_non_character_auxiliary_nodes(monkeypatch):
    """辅助节点不存在、或者同名但不是字符设备 → 跳过，不塞进 ``--device``。"""
    _fake_dev(monkeypatch, 'davinci[0-9]+', extra={
        'devmm_svm': _FakeStat(stat.S_IFREG | 0o644, 0),  # 同名普通文件
    }, absent=('hisi_hdc',))

    paths = devices.node_paths()

    assert '/dev/davinci_manager' in paths
    assert '/dev/devmm_svm' not in paths
    assert '/dev/hisi_hdc' not in paths


def test_auxiliary_node_paths_reports_what_exists(monkeypatch):
    _fake_dev(monkeypatch, 'davinci[0-9]+')

    assert devices.auxiliary_node_paths() == [
        '/dev/davinci_manager', '/dev/devmm_svm', '/dev/hisi_hdc',
    ]


# ── 和调用点对上 ─────────────────────────────────────────────────

def test_container_host_config_receives_the_auxiliary_nodes(monkeypatch, tmp_path):
    """真正 ``--device`` 的地方拿到的是 ``node_paths()`` 的完整结果。

    变异: ``execution/docker.py`` 里改成 ``scan_nodes()``（或自己拼路径），
    这条会红 —— 上面几条只证明了 ``node_paths`` 对，这条证明它被用上了。
    """
    from neu_box.execution.docker import DockerCommandExecutor

    _fake_dev(monkeypatch, 'davinci[0-9]+')
    monkeypatch.setattr(
        'neu_box.execution.docker.SbxManager.get_instance',
        classmethod(lambda cls: object()),
    )
    captured = {}

    class _API:
        def create_host_config(self, **options):
            captured.update(options)
            return {}

        def create_container(self, _image, **_options):
            return {'Id': 'container-id'}

        def start(self, _container_id):
            pass

    class _Client:
        api = _API()

    executor = DockerCommandExecutor(
        task={'target_spec': {'image': 'image:tag'}, 'command': 'echo ok'},
        sandbox_name='sbx_yuxd_task-1.slice', devices=[],
        log_path=str(tmp_path / 'task.log'),
    )

    executor._start_container(_Client())

    assert captured['devices'] == devices.node_paths()
    assert '/dev/davinci_manager' in captured['devices']
