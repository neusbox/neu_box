"""沙盒服务 API — 允许用户将当前终端进程加入独占设备的 cgroup 沙盒。

内网用户可通过 curl 或 neu-sbox 脚本调用：
  POST /sandbox/acquire   → 创建沙盒，把调用者的 shell PID 加入
  POST /sandbox/release   → 销毁沙盒，释放设备
  GET  /sandbox/list      → 列出自己的沙盒
"""

import logging
import os
import pwd

from flask import Blueprint, request

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
                    return line.strip().rsplit('/', 1)[-1]
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


@sandbox_bp.route('/acquire', methods=['POST'])
def acquire():
    """创建沙盒并加入指定 PID。

    请求体:
        { "username": "pengyt", "pid": 12345, "device_num": 1,
          "cpu": 0, "memory": 0, "mem_unit": "GB" }

    响应: { "sandbox_name": "user_pengyt_12345", "devices": ["235:0"], "message": "..." }
    """
    body = request.get_json(silent=True) or {}

    username = (body.get('username') or '').strip()
    pid = body.get('pid', 0)
    device_num = body.get('device_num', 0)
    device_ids = body.get('device_ids')  # 可选: ["1","3","5"]（minor 号）或 ["235:1","235:3"]（major:minor）
    cpu = body.get('cpu', 0)
    mem_val = body.get('memory', 0)
    mem_unit = body.get('mem_unit', 'GB')

    if not username or not pid:
        return {'error': 'username 和 pid 为必填参数'}, 400
    if not isinstance(pid, int) or pid <= 0:
        return {'error': 'pid 必须为正整数'}, 400

    # 校验 PID 归属
    if not _verify_pid_owner(pid, username):
        return {'error': f'PID {pid} 不属于用户 {username}，或进程不存在'}, 403

    # 如果 PID 已在某个沙盒中，先释放旧的（覆盖资源而非双占）
    sbx = SbxManager.get_instance()
    old_name = _find_sandbox_for_pid(pid)
    if old_name:
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

    # 把用户进程加入沙盒
    if not sbx.join_sandbox(sandbox_name, pid):
        sbx.destroy_sandbox(sandbox_name)
        return {'error': '加入沙盒失败'}, 500

    logger.warning("用户 %s PID %s 已加入沙盒 '%s'，独占设备 %s",
                   username, pid, sandbox_name, devices)

    return {
        'sandbox_name': sandbox_name,
        'devices': devices,
        'message': f'PID {pid} 已加入沙盒 {sandbox_name}，独占设备 {devices}',
    }, 201


@sandbox_bp.route('/release', methods=['POST'])
def release():
    """销毁沙盒，释放设备。

    请求体: { "sandbox_name": "user_pengyt_12345" }
    """
    body = request.get_json(silent=True) or {}
    sandbox_name = (body.get('sandbox_name') or '').strip()

    if not sandbox_name:
        return {'error': 'sandbox_name 为必填参数'}, 400

    sbx = SbxManager.get_instance()
    ok = sbx.destroy_sandbox(sandbox_name)
    if ok:
        logger.warning("沙盒 '%s' 已手动释放", sandbox_name)
        return {'message': f'沙盒 {sandbox_name} 已销毁', 'sandbox_name': sandbox_name}, 200
    else:
        return {'message': f'沙盒 {sandbox_name} 不存在或已销毁', 'sandbox_name': sandbox_name}, 200


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

    Query: ?username=pengyt  可选，过滤指定用户

    响应: { sandboxes: [{ name, owner, cpu, mem, devices, created_at, pids }] }
    """
    username = (request.args.get('username') or '').strip()
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

    return {'sandboxes': sandboxes}, 200
