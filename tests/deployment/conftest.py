"""第 3 层实机验收的 pytest 夹具与收集隔离。

**这一层不跳过。** 每组一个夹具，夹具里检查该组的前置条件，缺什么就直接
``pytest.fail`` 并把缺的东西写进消息 —— 这是部署验收，不是开发机上的便利
测试。``tests/integration/`` 那两层的 skip 语义在这里是反的。

收集隔离：这套用例默认**不参与**开发机上的 ``pytest tests/`` —— 它打真实
任务、真实设备、真实容器，最后几条用例还会停/起 Worker（SIGTERM MainPID +
``setup``）。只有显式设置 ``NEU_BOX_DEPLOYMENT_TESTS=1``（``neuboxctl
test`` 与 ``tests/deployment/run.py`` 都会设置）时才收集。
"""

from __future__ import annotations

import os

import pytest

from deployment_support import (  # noqa: E402  (同目录，pytest 已把它放进 sys.path)
    DEFAULT_CONFIG,
    DEFAULT_MANIFEST,
    DEFAULT_SERVICE,
    DEFAULT_URL,
    DEFAULT_CTL,
    REAPER_TIMEOUT,
    TASK_TIMEOUT,
    Deployment,
)
import deployment_support as support

_SELECTED = os.environ.get("NEU_BOX_DEPLOYMENT_TESTS") == "1"

# 非验收模式下这个目录里什么都不收：开发机上 `pytest tests/` 必须保持干净。
collect_ignore_glob: list[str] = [] if _SELECTED else ["test_*.py"]


def pytest_addoption(parser) -> None:
    group = parser.getgroup("deployment", "第 3 层实机验收")
    group.addoption(
        "--url", "--deployment-url", dest="deployment_url", default=DEFAULT_URL,
        help=f"Worker 地址（默认 {DEFAULT_URL}）",
    )
    group.addoption(
        "--user", "--deployment-user", dest="deployment_user", default="",
        help="执行任务的 Linux 用户（默认当前用户）",
    )
    group.addoption(
        "--deployment-config", dest="deployment_config", default=DEFAULT_CONFIG,
        help=f"Worker 环境配置文件（默认 {DEFAULT_CONFIG}）",
    )
    group.addoption(
        "--deployment-manifest", dest="deployment_manifest", default=DEFAULT_MANIFEST,
        help=f"安装清单（默认 {DEFAULT_MANIFEST}）",
    )
    group.addoption(
        "--deployment-service", dest="deployment_service", default=DEFAULT_SERVICE,
        help=f"Worker 的 systemd 单元（默认 {DEFAULT_SERVICE}）",
    )
    group.addoption(
        "--deployment-ctl", dest="deployment_ctl",
        default=DEFAULT_CTL,
        help=f"管理 CLI 路径，起服用 `neuboxctl setup`（默认 {DEFAULT_CTL}）",
    )
    group.addoption(
        "--task-timeout", dest="task_timeout", type=float, default=TASK_TIMEOUT,
        help=f"单个任务的等待上限，秒（默认 {TASK_TIMEOUT:.0f}）",
    )
    group.addoption(
        "--reaper-timeout", dest="reaper_timeout", type=float, default=REAPER_TIMEOUT,
        help=f"等 Reaper 回收沙盒的上限，秒（默认 {REAPER_TIMEOUT:.0f}）",
    )


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "deployment_restart: 会停/起 Worker 或让调度暂停，统一排到整套最后跑",
    )


def pytest_report_header(config) -> list[str]:
    if not _SELECTED:
        return []
    return [
        f"实机验收目标: {config.getoption('deployment_url')}",
        f"执行用户:     {config.getoption('deployment_user') or support.current_user()}",
    ]


def pytest_collection_modifyitems(session, config, items) -> None:
    """把重启类用例挪到最后。

    它们会停/起 Worker，跑在中间会把并发用例的沙盒和任务一起带走；而
    ``POST /maintenance/pause`` 之后 Worker 必须重启才能恢复调度。
    """
    restart = [item for item in items if item.get_closest_marker("deployment_restart")]
    if not restart:
        return
    items[:] = [item for item in items if item not in restart] + restart


@pytest.fixture(scope="session")
def deployment(request) -> Deployment:
    """第 3 层要操作的对象集合；只构造，不做前置判定。"""
    target = Deployment(
        url=request.config.getoption("deployment_url"),
        user=request.config.getoption("deployment_user"),
        config=request.config.getoption("deployment_config"),
        manifest=request.config.getoption("deployment_manifest"),
        service=request.config.getoption("deployment_service"),
        ctl=request.config.getoption("deployment_ctl"),
        task_timeout=request.config.getoption("task_timeout"),
        reaper_timeout=request.config.getoption("reaper_timeout"),
    )
    request.addfinalizer(target.cleanup)
    return target


@pytest.fixture(scope="session")
def basic(deployment: Deployment) -> Deployment:
    """基本盘：Worker 在线、API 版本够、测试用户存在、安装清单可读。"""
    try:
        health = deployment.client.healthz()
    except support.WorkerUnreachable as exc:
        pytest.fail(
            f"Worker 不可达（{deployment.url}）: {exc}\n"
            f"前置缺失：请先确认 systemctl status {deployment.service} 正常、"
            f"端口未被防火墙拦下",
            pytrace=False,
        )
    if health.status != 200:
        pytest.fail(
            f"{deployment.url}/healthz 返回 HTTP {health.status}，Worker 未就绪:\n"
            f"{health.text[:1000]}",
            pytrace=False,
        )
    payload = health.json()
    api_version = payload.get("api_version")
    if not isinstance(api_version, int) or api_version < 2:
        pytest.fail(
            f"/healthz 的 api_version={api_version!r}，本层验收要求 >= 2",
            pytrace=False,
        )
    if payload.get("role") != "worker":
        pytest.fail(
            f"/healthz 的 role={payload.get('role')!r}，应为 'worker'",
            pytrace=False,
        )
    if not support.user_exists(deployment.user):
        pytest.fail(
            f"前置缺失：系统用户 {deployment.user!r} 不存在；"
            f"用 --user 指定一个 Worker 宿主机上真实存在的用户",
            pytrace=False,
        )
    status = deployment.status()
    for field in ("total_devices", "idle_devices", "active_sandboxes", "dev_status"):
        if field not in status:
            pytest.fail(f"/status 缺少字段 {field!r}：{status}", pytrace=False)
    deployment.manifest()  # 缺失直接失败
    return deployment


@pytest.fixture(scope="session")
def single_card(deployment: Deployment, basic: Deployment) -> Deployment:
    """单卡组：至少 1 张空闲设备。

    空闲设备数是 Worker 的 fail-closed 判定结果：设备状态脚本跑不出来
    （``NEU_BOX_DEVICE_INFO_SCRIPT`` 失败或报 total=0）时空闲数恒为 0，
    所以这条前置同时覆盖了"驱动/状态脚本可用"。
    """
    idle = deployment.idle_devices()
    total = deployment.total_devices()
    if total <= 0:
        pytest.fail(
            f"前置缺失：Worker 没发现任何受管设备（total_devices=0）；"
            f"检查 {deployment.config_path} 的 NEU_BOX_DEVICE_FILTER "
            f"（当前 {deployment.device_filter!r}）与 /dev 下的设备节点",
            pytrace=False,
        )
    if idle < 1:
        pytest.fail(
            f"前置缺失：没有空闲设备（idle_devices=0，total={total}）；"
            f"本层不跳过 —— 要么让别的任务退出，要么先在维护窗口确认"
            f"NEU_BOX_DEVICE_INFO_SCRIPT 能跑出空闲卡",
            pytrace=False,
        )
    nodes = deployment.device_nodes()
    missing = [minor for minor in range(total) if minor not in nodes]
    if missing:
        pytest.fail(
            f"前置缺失：/dev 下找不到 minor={missing} 的受管设备节点；"
            f"NEU_BOX_DEVICE_FILTER={deployment.device_filter!r}",
            pytrace=False,
        )
    return deployment


@pytest.fixture(scope="session")
def multi_card(deployment: Deployment, single_card: Deployment) -> Deployment:
    """多卡组：至少 2 张空闲设备。"""
    idle = deployment.idle_devices()
    if idle < 2:
        pytest.fail(
            f"前置缺失：多卡用例需要 2 张空闲设备，当前只有 {idle} 张；"
            f"本层不跳过",
            pytrace=False,
        )
    return deployment


@pytest.fixture(scope="session")
def container(deployment: Deployment, basic: Deployment) -> Deployment:
    """容器组：dockerd 可用，且 default-runtime 指向 Neu Box 的 OCI runtime。

    容器能不能拿到设备，取决于 OCI runtime hook 有没有被真的执行 —— 只有
    dockerd 的 default-runtime 是 ``neu-box-runtime`` 才谈得上后面的用例。
    这个名字是 runtime 侧（neu_box_runtime）的契约，不是短名 ``neu-box``。
    """
    runtime = deployment.default_runtime()
    expected = os.environ.get("NEU_BOX_CONTAINER_RUNTIME", "neu-box-runtime")
    if runtime != expected:
        pytest.fail(
            f"前置缺失：dockerd 的 default-runtime 是 {runtime or '(空)'!r}，"
            f"应为 {expected!r}；此时 OCI runtime hook 不会被调用，"
            f"容器登记不可能发生。修法见 neu_box_runtime 的 "
            f"/etc/docker/daemon.json（runtimes + default-runtime，改完必须"
            f"restart docker）",
            pytrace=False,
        )
    return deployment


@pytest.fixture(scope="session")
def container_image(container: Deployment) -> str:
    """容器组要用的镜像：本机已有，自带 shell，优先 NEU_BOX_CONTAINER_IMAGE。

    镜像里的 ENTRYPOINT 一律被 ``--entrypoint sh`` 顶掉，所以这里只要求
    ``/bin/sh`` 能用 —— 用例要在容器里跑探测脚本。
    """
    image = container.resolve_image()
    probe = container.docker_run(
        "--rm", "--entrypoint", "sh", image, "-c", "echo neu-box-image-ok",
    )
    if probe.returncode != 0 or "neu-box-image-ok" not in (probe.stdout or ""):
        pytest.fail(
            f"前置缺失：镜像 {image!r} 起不来或没有可用的 /bin/sh"
            f"（docker run 退出码 {probe.returncode}）:\n"
            f"{(probe.stdout or '')[:1500]}\n"
            f"容器组用例要在容器里执行探测脚本；用 NEU_BOX_CONTAINER_IMAGE "
            f"指定一个带 shell 的镜像",
            pytrace=False,
        )
    return image


@pytest.fixture(scope="session")
def slow(deployment: Deployment, single_card: Deployment) -> Deployment:
    """慢组：Reaper 类用例，需要 1 张空闲设备和 Worker 的收尸线程在跑。"""
    maintenance = deployment.client.maintenance()
    if maintenance.status != 200:
        pytest.fail(
            f"GET /maintenance 失败（HTTP {maintenance.status}）: "
            f"{maintenance.text[:1000]}",
            pytrace=False,
        )
    return deployment
