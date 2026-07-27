from flask import Blueprint, request
from src_manager.nodes_pool import Nodes_Pool
from entry_point.auth import login_required

terminal_bp = Blueprint('terminal', __name__)

@terminal_bp.route('/create', methods=['POST'])
@login_required
def create():
    data = request.get_json()

    if not data:
        return {'error': '请求体不能为空'}, 400

    node_id = data.get('node_id')
    cpu = data.get('cpu', 1)
    memory = data.get('memory', 1)
    mem_unit = data.get('mem_unit', 'GB')
    device_num = data.get('device_num', 0)
    username = data.get('username', '')
    password = data.get('password', '')

    # 参数校验
    errors = []
    if not isinstance(cpu, int) or cpu < 0:
        errors.append('CPU 不能为负数')
    if not isinstance(memory, int) or memory < 0:
        errors.append('内存不能为负数')
    if mem_unit not in ('MB', 'GB'):
        errors.append('内存单位只能是 MB 或 GB')
    if not isinstance(device_num, int) or device_num < 0:
        errors.append('设备数不能为负数')
    if not username or not isinstance(username, str) or not username.strip():
        errors.append('用户名不能为空')
    if not password or not isinstance(password, str):
        errors.append('密码不能为空')

    if errors:
        return {'error': '; '.join(errors)}, 400

    req = {
        'cpu': cpu,
        'memory': memory,
        'mem_unit': mem_unit,
        'device_num': device_num,
        'device_ids': data.get('device_ids'),
        'username': username,
        'password': password,
    }

    try:
        response = Nodes_Pool.get_nodes_pool().req_node(node_id, req)
    except ValueError as e:
        return {'error': f'{e}。请检查左侧是否选择了正确的节点'}, 404

    return response.json(), response.status_code