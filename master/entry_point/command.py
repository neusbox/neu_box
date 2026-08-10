"""Master 命令执行入口 — 将前端请求转发到对应 Worker。"""

import requests
from flask import Blueprint, request
from src_manager.nodes_pool import Nodes_Pool
from entry_point.auth import login_required

command_bp = Blueprint('command', __name__)


@command_bp.route('/run', methods=['POST'])
@login_required
def run():
    """提交命令到指定 Worker 的任务队列。"""
    data = request.get_json(silent=True) or {}

    if not data:
        return {'error': '请求体不能为空'}, 400

    node_id = (data.get('node_id') or '').strip()
    if not node_id:
        return {'error': 'node_id 不能为空'}, 400

    command = (data.get('command') or '').strip()
    if not command:
        return {'error': '命令不能为空'}, 400

    user_id = (data.get('user_id') or '').strip()
    if not user_id:
        return {'error': 'user_id 不能为空'}, 400

    req = {
        'command': command,
        'user_id': user_id,
        'cpu': data.get('cpu', 0),
        'memory': data.get('memory', 0),
        'mem_unit': data.get('mem_unit', 'GB'),
        'device_num': data.get('device_num', 0),
        'device_ids': data.get('device_ids'),
        'est_time': data.get('est_time', 0),
        'target': data.get('target'),
    }

    try:
        resp = Nodes_Pool.get_nodes_pool().forward_to_node(node_id, '/command/run', req)
        return resp.json(), resp.status_code
    except ValueError as e:
        return {'error': f'{e}。请检查是否选择了正确的节点'}, 404


@command_bp.route('/tasks/delete', methods=['POST'])
@login_required
def delete_tasks():
    """批量删除任务，转发到指定 Worker。

    Body: { "node_id": "...", "task_ids": [...] }
    """
    data = request.get_json(silent=True) or {}
    node_id = (data.get('node_id') or '').strip()
    if not node_id:
        return {'error': 'node_id 不能为空'}, 400

    task_ids = data.get('task_ids') or []
    if not task_ids:
        return {'error': 'task_ids 不能为空'}, 400

    try:
        resp = Nodes_Pool.get_nodes_pool().forward_to_node(
            node_id, '/command/tasks/delete', {'task_ids': task_ids})
        return resp.json(), resp.status_code
    except ValueError as e:
        return {'error': f'{e}。请检查是否选择了正确的节点'}, 404


@command_bp.route('/queue', methods=['GET'])
@login_required
def queue():
    """查看指定 Worker 上的任务队列。

    Query: ?node_id=xxx
    """
    node_id = (request.args.get('node_id') or '').strip()
    if not node_id:
        return {'error': 'node_id 参数必填'}, 400

    try:
        resp = Nodes_Pool.get_nodes_pool().forward_get_to_node(node_id, '/command/queue')
        return resp.json(), resp.status_code
    except ValueError as e:
        return {'error': f'{e}。请检查是否选择了正确的节点'}, 404


@command_bp.route('/result/<task_id>', methods=['GET'])
@login_required
def result(task_id: str):
    """查看任务结果元数据（状态、返回码等）。

    Query: ?node_id=xxx
    """
    node_id = (request.args.get('node_id') or '').strip()
    if not node_id:
        return {'error': 'node_id 参数必填'}, 400

    try:
        resp = Nodes_Pool.get_nodes_pool().forward_get_to_node(
            node_id, f'/command/result/{task_id}')
        return resp.json(), resp.status_code
    except ValueError as e:
        return {'error': f'{e}。请检查是否选择了正确的节点'}, 404


@command_bp.route('/result/<task_id>/log', methods=['GET'])
@login_required
def result_log(task_id: str):
    """获取任务日志文件内容（分块）。

    Query: ?node_id=xxx&tail=N 或 &offset=N&limit=M
    代理到 worker 的 /command/result/<id>/log
    """
    node_id = (request.args.get('node_id') or '').strip()
    if not node_id:
        return {'error': 'node_id 参数必填'}, 400

    params = {}
    for key in ('offset', 'limit', 'tail', 'raw'):
        val = request.args.get(key)
        if val is not None:
            params[key] = val

    try:
        resp = Nodes_Pool.get_nodes_pool().forward_get_to_node(
            node_id, f'/command/result/{task_id}/log', params=params or None)
        # raw 模式 → 透明转发纯文本，不解析 JSON
        if request.args.get('raw'):
            return resp.text, resp.status_code, {'Content-Type': resp.headers.get('Content-Type', 'text/plain')}
        return resp.json(), resp.status_code
    except ValueError as e:
        return {'error': f'{e}。请检查是否选择了正确的节点'}, 404
