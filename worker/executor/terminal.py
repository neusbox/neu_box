import logging
import os
import platform
import pwd
import subprocess
import time

from flask import Blueprint, request
from executor.port_pool import Port_Pool_Manager
from executor.sbx_manager import SbxManager

logger = logging.getLogger(__name__)
terminal_bp = Blueprint('terminal', __name__)


def _resolve_ttyd_binary():
    """根据系统架构自动选择 worker/deps/ 下对应的 ttyd 二进制。"""
    deps_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'deps'))
    machine = platform.machine()
    arch_map = {
        'aarch64': 'ttyd.aarch64',
        'x86_64':  'ttyd.x86_64',
    }
    filename = arch_map.get(machine)
    if filename:
        path = os.path.join(deps_dir, filename)
        if os.path.isfile(path):
            logger.info('使用内嵌二进制: %s', path)
            return path
    # 架构未知或文件缺失，回退到系统 PATH
    logger.warning('未找到匹配的架构 (%s)，回退到系统 ttyd', machine)
    return 'ttyd'


TTYD_BIN = _resolve_ttyd_binary()


@terminal_bp.route('/create', methods=['POST'])
def create():
    # 解析请求中的设备/资源需求
    body = request.get_json(silent=True) or {}
    device_num = body.get('device_num', 0)
    device_ids = body.get('device_ids')  # 可选，用户指定卡号
    sandbox_cpu = body.get('cpu', 0)
    # 将 memory(int) + mem_unit 转为沙盒用的字符串格式（如 "2G", "512M", "0"=不限）
    mem_val = body.get('memory', 0)
    mem_unit = body.get('mem_unit', 'GB')
    if mem_val == 0:
        sandbox_mem = '0'
    elif mem_unit == 'GB':
        sandbox_mem = f'{mem_val}G'
    else:
        sandbox_mem = f'{mem_val}M'
    username = body.get('username', '')
    password = body.get('password', '')

    # 1. 临时占住一个端口，传入一个虚拟的 PID (比如 -1)
    # 因为此时进程还没启动，还没拿到真实 PID
    port = Port_Pool_Manager.get_Port_Pool_Manager().acquire_port()
    if not port:
        return {'error': '服务器繁忙', 'details': '防火墙端口池已耗尽，请稍后再试'}, 503

    try:
        # 2. 构建 ttyd 命令
        ttyd_cmd = [TTYD_BIN, '-p', str(port), '-W', '-q']

        # 如果提供了用户名和密码，启用 HTTP 基本认证
        if username and password:
            ttyd_cmd.extend(['-c', f'{username}:{password}'])

        # 如果指定了用户名，使用 ttyd 原生 -u/-g/-w 切换用户及工作目录
        if username:
            pw = pwd.getpwnam(username)
            ttyd_cmd.extend(['-u', str(pw.pw_uid),
                             '-g', str(pw.pw_gid),
                             '-w', pw.pw_dir])
            # 显式设置 HOME，否则 bash 会尝试读 /root/.bashrc 导致 Permission denied
            ttyd_cmd.extend(['env', f'HOME={pw.pw_dir}', 'bash'])
        else:
            ttyd_cmd.append('bash')

        # 3. 启动 ttyd，绑定拿到的黄金端口
        process = subprocess.Popen(
            ttyd_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # 4. 稍微等待，确保没秒退
        time.sleep(0.5)
        if process.poll() is not None:
            # 如果启动失败，读取 stderr 获取真实错误原因
            stderr_output = ''
            try:
                stderr_output = process.stderr.read().decode('utf-8', errors='replace').strip()
            except Exception:
                pass
            Port_Pool_Manager.get_Port_Pool_Manager().release_port(port)
            detail = stderr_output or f'ttyd 异常退出 (exit code={process.returncode})'
            logger.error('ttyd 启动失败: %s', detail)
            return {'error': '终端启动失败', 'details': detail}, 500

        # 5. 归一化 device_ids → ["major:minor", ...]
        sbx = SbxManager.get_instance()
        normalized_ids = None
        if device_ids and isinstance(device_ids, list):
            from executor.sandbox_api import _normalize_device_ids
            normalized_ids = _normalize_device_ids(device_ids, sbx._discover_device_nodes())

        # 6. 分配沙盒并加入 ttyd 进程
        owner = username if username else 'root'
        result = sbx.allocate_for_terminal(
            owner=owner,
            terminal_id=str(process.pid),
            cpu=sandbox_cpu,
            mem=sandbox_mem,
            device_num=device_num if not normalized_ids else 0,
            device_ids=normalized_ids,
            port=port,
        )
        if result is None:
            # 沙盒创建失败 → 清理 ttyd 并归还端口
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()
            Port_Pool_Manager.get_Port_Pool_Manager().release_port(port)
            return {'error': '沙盒分配失败', 'details': '无法创建 cgroup 沙盒，请检查系统配置'}, 500

        sandbox_name = result['sandbox_name']
        allocated_devices = result['devices']

        # 将 ttyd 进程加入沙盒
        if not sbx.join_sandbox(sandbox_name, process.pid):
            # 加入失败 → 清理沙盒、ttyd、端口
            sbx.destroy_sandbox(sandbox_name)
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()
            Port_Pool_Manager.get_Port_Pool_Manager().release_port(port)
            return {'error': '沙盒加入失败', 'details': '无法将进程加入沙盒'}, 500

        # 6. 启动成功，更新池子里记录的真实 PID

        # 自动获取本机 IP 地址（适配多网卡/容器环境）,使用ip addr show 命令获取非回环地址
        ip = os.getenv('HOST_IP')  # 优先使用环境变量指定的 IP

        return {
            'message': '终端创建成功',
            'sandbox_id': process.pid,
            'sandbox_name': sandbox_name,
            'allocated_devices': allocated_devices,
            'assigned_port': port,
            'terminal_url': f'http://{ip}:{port}'
        }, 201

    except Exception as e:
        # 万一代码抛出未知异常，确保端口一定会被释放（防死锁/防堆积）
        Port_Pool_Manager.get_Port_Pool_Manager().release_port(port)
        return {'error': '系统内部错误', 'details': str(e)}, 500
