"""沙盒服务 API — 允许用户将当前终端进程加入独占设备的 cgroup 沙盒。

内网用户可通过 HTTP 或 neu-sbox 客户端调用：
  POST /sandbox/acquire   → 创建沙盒，把调用者的 shell PID 加入
  POST /sandbox/release   → 销毁沙盒，释放设备
  GET  /sandbox/list      → 列出自己的沙盒
"""

import logging
import os
import pwd

from flask import Blueprint, request

from neu_box.worker.executor.command_docker import (
    DockerExecutorError,
    _expected_cgroup,
    _read_unified_cgroup,
    resolve_container_pid,
)
from neu_box.worker.executor.container_terminal import (
    join_container_terminal,
    restore_container_terminal,
)
from neu_box.worker.executor.db import Database
from neu_box.worker.executor.sbx_manager import SbxManager

logger = logging.getLogger(__name__)
sandbox_bp = Blueprint('sandbox', __name__)


def _normalize_device_ids(raw: list, all_devices: list[str]) -> list[str]:
    """将用户指定的设备 ID 归一化为 ["major:minor", ...] 格式。

    支持两种输入：
      - 纯数字: ["1","3"] → 在所有设备中匹配 minor 号 → ["235:1","235:3"]
      - major:minor: ["235:1","235:3"] → 原样返回
    """
    result = []
    for d in raw:
        d = str(d).strip()
        if not d:
            continue
        if ':' in d:
            result.append(d)
        else:
            # 纯数字 → 在所有设备中匹配 minor 号
            for dev in all_devices:
                if dev.endswith(f':{d}'):
                    result.append(dev)
                    break
    return result if result else None


def _find_sandbox_for_pid(pid: int) -> str | None:
    """通过 /proc/<pid>/cgroup 查找 PID 所在的沙盒名称。"""
    try:
        with open(f'/proc/{pid}/cgroup') as f:
            for line in f:
                # cgroup v2 格式: "0::/sandbox_sbx_pengyt_12345.slice"
                if 'sandbox_' in line:
                    leaf = line.strip().rsplit('/', 1)[-1]
                    return (
                        leaf[len('sandbox_'):]
                        if leaf.startswith('sandbox_') else leaf
                    )
    except Exception:
        pass
    return None


def _verify_pid_owner(pid: int, username: str) -> bool:
    """校验 PID 是否属于 username。root 进程（如 Docker 容器）直接放行。"""
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('Uid:'):
                    real_uid = int(line.split()[1])
                    if real_uid == 0:          # root → Docker 容器等，放行
                        return True
                    pw = pwd.getpwnam(username)
                    return real_uid == pw.pw_uid
    except Exception:
        pass
    return False


def _container_ref(body: dict) -> str:
    """读取可选 container 字段；当前只接受容器名称或 ID 字符串。"""
    value = body.get('container')
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('container 必须是容器名称或 ID 字符串')
    return value.strip()


def _docker_error_status(exc: DockerExecutorError) -> int:
    if exc.code == 'docker_container_not_found':
        return 404
    if exc.code in {'docker_unavailable', 'docker_sdk_unavailable'}:
        return 503
    if exc.code in {
        'docker_container_changed',
        'docker_container_pid_changed',
        'docker_container_pid_namespace_mismatch',
        'docker_container_pid_cgroup_mismatch',
        'docker_container_release_identity_mismatch',
        'docker_container_release_cgroup_mismatch',
        'docker_devices_not_visible',
    }:
        return 409
    if exc.code in {
        'docker_container_pid_join_failed',
        'docker_container_pid_cgroup_verify_failed',
        'docker_container_pid_stop_timeout',
        'docker_container_release_restore_failed',
    }:
        return 500
    return 400


def _docker_error_response(exc: DockerExecutorError):
    return {'error': str(exc), 'code': exc.code}, _docker_error_status(exc)


@sandbox_bp.route('/acquire', methods=['POST'])
def acquire():
    """创建沙盒并加入指定 PID。

    请求体:
        { "username": "pengyt", "pid": 12345, "device_num": 1,
          "cpu": 0, "memory": 0, "mem_unit": "GB",
          "container": "oprace" }

    container 缺省时 pid 为宿主机 PID；提供 container 时 pid 为容器 PID。

    响应: { "sandbox_name": "user_pengyt_12345", "devices": ["235:0"], "message": "..." }
    """
    body = request.get_json(silent=True) or {}

    username = (body.get('username') or '').strip()
    requested_pid = body.get('pid', 0)
    device_num = body.get('device_num', 0)
    device_ids = body.get('device_ids')  # 可选: ["1","3","5"]（minor 号）或 ["235:1","235:3"]（major:minor）
    cpu = body.get('cpu', 0)
    mem_val = body.get('memory', 0)
    mem_unit = body.get('mem_unit', 'GB')

    try:
        container = _container_ref(body)
    except ValueError as exc:
        return {'error': str(exc)}, 400

    if not username or not requested_pid:
        return {'error': 'username 和 pid 为必填参数'}, 400
    if not isinstance(requested_pid, int) or requested_pid <= 0:
        return {'error': 'pid 必须为正整数'}, 400

    container_process = None
    if container:
        try:
            container_process = resolve_container_pid(
                container, requested_pid,
            )
        except DockerExecutorError as exc:
            return _docker_error_response(exc)
        pid = container_process.host_pid
    else:
        pid = requested_pid
        if not _verify_pid_owner(pid, username):
            return {
                'error': f'PID {pid} 不属于用户 {username}，或进程不存在'
            }, 403

    # 如果 PID 已在某个沙盒中，先释放旧的（覆盖资源而非双占）
    sbx = SbxManager.get_instance()
    old_name = _find_sandbox_for_pid(pid)
    if old_name:
        if container_process is not None:
            return {
                'error': (
                    f'容器 PID {requested_pid} 已在沙盒 {old_name} 中；'
                    '请先 release'
                ),
                'sandbox_name': old_name,
            }, 409
        logger.warning("PID %s 已在沙盒 '%s' 中，先释放旧沙盒", pid, old_name)
        sbx.destroy_sandbox(old_name)

    # 转换内存格式
    if mem_val == 0:
        sandbox_mem = '0'
    elif mem_unit == 'GB':
        sandbox_mem = f'{mem_val}G'
    else:
        sandbox_mem = f'{mem_val}M'

    # 归一化 device_ids → ["major:minor", ...]
    normalized_ids = None
    if device_ids and isinstance(device_ids, list):
        normalized_ids = _normalize_device_ids(device_ids, sbx._discover_device_nodes())

    # 创建沙盒并分配设备
    sbx = SbxManager.get_instance()
    result = sbx.allocate_sandbox(
        owner=username,
        sandbox_id=str(pid),
        cpu=cpu,
        mem=sandbox_mem,
        device_num=device_num if not normalized_ids else 0,
        device_ids=normalized_ids,
    )
    if result is None:
        return {'error': '沙盒创建失败，设备可能不足'}, 503

    sandbox_name = result['sandbox_name']
    devices = result['devices']

    if container_process is not None:
        visible = set(sbx._discover_device_nodes(
            f'/proc/{container_process.init_host_pid}/root/dev',
        ))
        missing = sorted(set(devices) - visible)
        if missing:
            sbx.destroy_sandbox(sandbox_name)
            exc = DockerExecutorError(
                f'目标容器没有挂载沙盒设备节点: {missing}',
                'docker_devices_not_visible',
            )
            return _docker_error_response(exc)

        try:
            join_container_terminal(sbx, sandbox_name, container_process)
        except DockerExecutorError as exc:
            # 回滚已把 shell 迁出时可以安全销毁；若回滚失败则保留现场，
            # 避免 destroy 的 cgroup.kill 杀死交互终端。
            safe_to_destroy = False
            try:
                safe_to_destroy = (
                    _read_unified_cgroup(pid)
                    != _expected_cgroup(sandbox_name)
                )
            except (DockerExecutorError, OSError):
                logger.exception(
                    "无法确认容器 shell PID %s 的 cgroup，保留沙盒 '%s'",
                    pid,
                    sandbox_name,
                )
            if safe_to_destroy:
                sbx.destroy_sandbox(sandbox_name)
            else:
                logger.error(
                    "容器 shell 可能仍在沙盒 '%s'，为避免误杀不执行 destroy",
                    sandbox_name,
                )
            return _docker_error_response(exc)
    elif not sbx.join_sandbox(sandbox_name, pid):
        sbx.destroy_sandbox(sandbox_name)
        return {'error': '加入沙盒失败'}, 500

    logger.warning(
        "用户 %s PID %s%s 已加入沙盒 '%s'，独占设备 %s",
        username,
        pid,
        (
            f' (container={container} pid={requested_pid})'
            if container_process is not None else ''
        ),
        sandbox_name,
        devices,
    )

    return {
        'sandbox_name': sandbox_name,
        'devices': devices,
        'message': (
            f'容器 {container} PID {requested_pid} 已加入沙盒 '
            f'{sandbox_name}，独占设备 {devices}'
            if container_process is not None
            else f'PID {pid} 已加入沙盒 {sandbox_name}，独占设备 {devices}'
        ),
    }, 201


@sandbox_bp.route('/release', methods=['POST'])
def release():
    """销毁沙盒，释放设备。

    Host 请求体: { "sandbox_name": "sbx_pengyt_12345.slice" }

    容器终端请求体:
        { "sandbox_name": "sbx_yuxd_43210.slice", "container": "oprace",
          "pid": 12, "client_pid": 34 }

    容器模式会先将交互 shell 与本次 release HTTP 客户端迁回 Docker
    cgroup，再销毁 sandbox；其余残留进程由 sandbox destroy 清理。
    """
    body = request.get_json(silent=True) or {}
    sandbox_name = (body.get('sandbox_name') or '').strip()

    try:
        container = _container_ref(body)
    except ValueError as exc:
        return {'error': str(exc)}, 400

    if not sandbox_name:
        return {'error': 'sandbox_name 为必填参数'}, 400

    sbx = SbxManager.get_instance()
    if container:
        shell_pid = body.get('pid', 0)
        client_pid = body.get('client_pid', 0)
        if not isinstance(shell_pid, int) or shell_pid <= 0:
            return {'error': '容器模式 release 的 pid 必须为正整数'}, 400
        if not isinstance(client_pid, int) or client_pid <= 0:
            return {
                'error': '容器模式 release 的 client_pid 必须为正整数'
            }, 400
        if shell_pid == client_pid:
            return {'error': 'pid 与 client_pid 不能相同'}, 400

        try:
            shell = resolve_container_pid(container, shell_pid)
            client = resolve_container_pid(container, client_pid)
            record = sbx.db.get_sandbox(sandbox_name)
            if not record or shell.host_pid not in record.get('pids', []):
                raise DockerExecutorError(
                    '当前容器 shell 不是该沙盒登记的终端进程',
                    'docker_container_release_identity_mismatch',
                )
            restore_container_terminal(
                sbx, sandbox_name, shell, client,
            )
        except DockerExecutorError as exc:
            return _docker_error_response(exc)

    ok = sbx.destroy_sandbox(sandbox_name)
    if ok:
        logger.warning("沙盒 '%s' 已手动释放", sandbox_name)
        return {'message': f'沙盒 {sandbox_name} 已销毁', 'sandbox_name': sandbox_name}, 200
    else:
        return {
            'error': f'沙盒 {sandbox_name} 销毁失败',
            'sandbox_name': sandbox_name,
        }, 500


@sandbox_bp.route('/join', methods=['POST'])
def join():
    """将指定 PID 加入已有沙盒。

    请求体: { "username": "pengyt", "pid": 12345, "sandbox_name": "sbx_pengyt_67890.slice" }

    校验:
      1. PID 必须属于 username（通过 /proc/<pid>/status 的 UID 校验）
      2. 沙盒名的 owner 段必须与 username 一致
    """
    body = request.get_json(silent=True) or {}
    username = (body.get('username') or '').strip()
    pid = body.get('pid', 0)
    sandbox_name = (body.get('sandbox_name') or '').strip()

    if not username or not pid:
        return {'error': 'username 和 pid 为必填参数'}, 400
    if not isinstance(pid, int) or pid <= 0:
        return {'error': 'pid 必须为正整数'}, 400
    if not sandbox_name:
        return {'error': 'sandbox_name 为必填参数'}, 400

    # 校验 PID 归属
    if not _verify_pid_owner(pid, username):
        return {'error': f'PID {pid} 不属于用户 {username}，或进程不存在'}, 403

    # 校验沙盒归属：从沙盒名提取 owner
    if sandbox_name.startswith('sbx_'):
        parts = sandbox_name[:-6].split('_', 2) if sandbox_name.endswith('.slice') else sandbox_name.split('_', 2)
        owner = parts[1] if len(parts) > 1 else ''
        if owner != username:
            return {'error': f'沙盒 "{sandbox_name}" 不属于用户 {username}（属于 {owner}）'}, 403
    else:
        return {'error': '不支持的沙盒名称格式'}, 400

    # 如果 PID 已在某个沙盒中，先退出旧的
    sbx = SbxManager.get_instance()
    old_name = _find_sandbox_for_pid(pid)
    if old_name:
        logger.warning("join: PID %s 已在沙盒 '%s' 中，先退出", pid, old_name)
        # 从旧沙盒 cgroup 中移除（移到根 cgroup）
        try:
            with open('/sys/fs/cgroup/cgroup.procs', 'w') as f:
                f.write(str(pid))
        except Exception:
            pass

    # 加入目标沙盒
    if not sbx.join_sandbox(sandbox_name, pid):
        return {'error': '加入沙盒失败'}, 500

    logger.warning("用户 %s 将 PID %s 加入沙盒 '%s'", username, pid, sandbox_name)
    return {
        'sandbox_name': sandbox_name,
        'pid': pid,
        'message': f'PID {pid} 已加入沙盒 {sandbox_name}',
    }, 200


@sandbox_bp.route('/list', methods=['GET'])
def list_sandboxes():
    """列出沙盒及其资源信息。

    Query:
      ?username=pengyt                         按用户过滤
      ?container=oprace&pid=12                 查询容器终端所在沙盒

    响应: { sandboxes: [...], current_sandbox: "sbx_..." | null }
    """
    username = (request.args.get('username') or '').strip()
    container = (request.args.get('container') or '').strip()
    container_pid = request.args.get('pid')

    current_sandbox = None
    if container or container_pid is not None:
        if not container or container_pid is None:
            return {'error': 'container 和 pid 必须同时提供'}, 400
        try:
            parsed_pid = int(container_pid)
        except (TypeError, ValueError):
            return {'error': 'pid 必须为正整数'}, 400
        if parsed_pid <= 0:
            return {'error': 'pid 必须为正整数'}, 400
        try:
            process = resolve_container_pid(container, parsed_pid)
        except DockerExecutorError as exc:
            return _docker_error_response(exc)
        current_sandbox = _find_sandbox_for_pid(process.host_pid)

    db = Database.get_instance()
    all_records = db.list_sandboxes()

    sandboxes = []
    for s in all_records:
        name = s.get('name') or ''
        if username and not name.startswith(f"sbx_{username}_"):
            continue
        # 从沙盒名提取 owner
        owner = ''
        if name.startswith('sbx_'):
            parts = name[:-6].split('_', 2) if name.endswith('.slice') else name.split('_', 2)
            owner = parts[1] if len(parts) > 1 else ''
        sandboxes.append({
            'name': name,
            'owner': owner,
            'cpu': s.get('cpu', 0),
            'mem': s.get('mem', '0'),
            'devices': s.get('devices', []),
            'created_at': s.get('created_at'),
            'pids': s.get('pids', []),
        })

    return {
        'sandboxes': sandboxes,
        'current_sandbox': current_sandbox,
    }, 200
