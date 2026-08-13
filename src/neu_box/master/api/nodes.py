import json
import os
import threading

import flask
from neu_box.master.services.nodes_pool import Nodes_Pool
from neu_box.master.api.auth import login_required
from neu_box.master.paths import nodes_config_path

nodes_bp = flask.Blueprint('nodes', __name__)

CONFIG_PATH = str(nodes_config_path())
_config_lock = threading.Lock()


def _read_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _write_config(cfg):
    with _config_lock:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())


# ── config.json 管理（必须注册在 /<node_id>/status 之前，否则 /config 会被当成 node_id） ──

@nodes_bp.route('/config', methods=['GET'])
def get_config_nodes():
    """返回 config.json 中的 nodes_pool 列表。"""
    cfg = _read_config()
    return {'nodes': cfg.get('nodes_pool', [])}, 200


@nodes_bp.route('/config/add', methods=['POST'])
@login_required
def add_config_node():
    """向 config.json 新增一个节点。"""
    data = flask.request.get_json() or {}
    name = (data.get('name', '') or '').strip()
    host = (data.get('host', '') or '').strip()
    port = data.get('port')

    errors = []
    if not name:
        errors.append('节点名称不能为空')
    if not host:
        errors.append('host 不能为空')
    if not isinstance(port, int) or port < 1 or port > 65535:
        errors.append('端口必须在 1-65535 之间')
    if errors:
        return {'error': '; '.join(errors)}, 400

    cfg = _read_config()
    nodes = cfg.setdefault('nodes_pool', [])

    # 检查名称是否重复
    if any(n.get('name') == name for n in nodes):
        return {'error': f"节点名称 '{name}' 已存在"}, 409

    nodes.append({'name': name, 'host': host, 'port': port})
    _write_config(cfg)

    # 通知 Nodes_Pool 立即同步
    Nodes_Pool.get_nodes_pool().sync_from_config()
    return {'message': f"节点 '{name}' 已添加"}, 201


@nodes_bp.route('/config/remove', methods=['POST'])
@login_required
def remove_config_node():
    """从 config.json 删除一个节点（按名称匹配）。"""
    data = flask.request.get_json() or {}
    name = (data.get('name', '') or '').strip()
    if not name:
        return {'error': '节点名称不能为空'}, 400

    cfg = _read_config()
    nodes = cfg.get('nodes_pool', [])
    before = len(nodes)
    cfg['nodes_pool'] = [n for n in nodes if n.get('name') != name]

    if len(cfg['nodes_pool']) == before:
        return {'error': f"节点 '{name}' 不存在"}, 404

    _write_config(cfg)

    # 通知 Nodes_Pool 立即同步
    Nodes_Pool.get_nodes_pool().sync_from_config()
    return {'message': f"节点 '{name}' 已删除"}, 200


# ── 运行时状态查询 ──────────────────────────────────────────

@nodes_bp.route('/get_all_nodes', methods=['POST'])
def get_all_nodes():
    """返回所有已注册节点的列表及当前状态，供前端选择器使用。
    每次请求时主动向所有 worker 查询一次实时状态，确保前端拿到最新数据。"""
    pool = Nodes_Pool.get_nodes_pool()
    pool.query_all_nodes_status()
    nodes_info = pool.get_all_nodes()
    return {'nodes': nodes_info}, 200


@nodes_bp.route('/<node_id>/status', methods=['GET'])
def query_node_status(node_id: str):
    """Master 主动查询某个 worker 的实时资源状态。"""
    pool = Nodes_Pool.get_nodes_pool()
    try:
        result = pool.query_node_status(node_id)
        return result, 200
    except ValueError as e:
        return {'error': str(e)}, 404


@nodes_bp.route('/<node_id>/sandboxes', methods=['GET'])
def list_node_sandboxes(node_id: str):
    """获取节点上所有活跃沙盒（终端 + 命令任务）。代理到 worker 的 GET /sandbox/list"""
    pool = Nodes_Pool.get_nodes_pool()
    try:
        resp = pool.forward_get_to_node(node_id, '/sandbox/list')
        return resp.json(), resp.status_code
    except ValueError as e:
        return {'error': str(e)}, 404
