"""Master 用户认证 — 登录/登出/凭据管理。

会话:
  - 登录后 session['user_id'] 标识当前用户
  - @login_required 装饰器拦截未登录请求

节点凭据:
  - 每个用户可为不同节点保存命令任务 username
  - 前端选中节点时自动填入已存凭据
"""

import functools
import logging

from flask import Blueprint, request, session

from src_manager.db import Database

auth_bp = Blueprint('auth', __name__)
db = Database.get_instance()
logger = logging.getLogger('master.auth')


def login_required(f):
    """装饰器：要求已登录，否则返回 401。"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            return {'error': '请先登录'}, 401
        return f(*args, **kwargs)
    return wrapper


def get_current_user() -> dict | None:
    """获取当前登录用户，未登录返回 None。"""
    uid = session.get('user_id')
    if not uid:
        return None
    return db.get_user(uid)


# ═══════════════════════════════════════════════════════════════
# 登录 / 登出
# ═══════════════════════════════════════════════════════════════

@auth_bp.route('/login', methods=['POST'])
def login():
    """登录。

    请求体: { "username": "...", "password": "..." }
    成功: { "user": { "id", "username", "role" }, "message": "..." }
    失败: { "error": "..." }, 401
    """
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return {'error': '用户名和密码不能为空'}, 400

    user = db.verify_user(username, password)
    if not user:
        logger.warning('登录失败: %s', username)
        return {'error': '用户名或密码错误'}, 401

    session.permanent = True
    session['user_id'] = user['id']
    logger.info('用户登录: %s (%s)', username, user['role'])

    return {
        'user': {
            'id': user['id'],
            'username': user['username'],
            'role': user['role'],
        },
        'message': '登录成功',
    }, 200


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """登出，清空 session。"""
    uid = session.pop('user_id', None)
    if uid:
        logger.info('用户登出: %s', uid)
    return {'message': '已登出'}, 200


@auth_bp.route('/me', methods=['GET'])
@login_required
def me():
    """返回当前登录用户信息。"""
    user = get_current_user()
    if not user:
        return {'error': '登录已过期'}, 401
    return {'user': user}, 200


# ═══════════════════════════════════════════════════════════════
# 修改密码
# ═══════════════════════════════════════════════════════════════

@auth_bp.route('/password', methods=['PUT'])
@login_required
def change_password():
    """修改当前用户密码。

    请求体: { "old_password": "...", "new_password": "..." }
    """
    data = request.get_json(silent=True) or {}
    old_password = data.get('old_password') or ''
    new_password = data.get('new_password') or ''

    if not old_password or not new_password:
        return {'error': '旧密码和新密码不能为空'}, 400
    if len(new_password) < 4:
        return {'error': '新密码至少 4 位'}, 400
    if old_password == new_password:
        return {'error': '新密码不能与旧密码相同'}, 400

    user = get_current_user()
    if not user:
        return {'error': '登录已过期'}, 401

    # 验证旧密码
    verified = db.verify_user(user['username'], old_password)
    if not verified:
        return {'error': '旧密码不正确'}, 403

    db.update_password(user['id'], new_password)
    logger.info('用户 %s 修改了密码', user['username'])
    return {'message': '密码已修改，请重新登录'}, 200


# ═══════════════════════════════════════════════════════════════
# 节点凭据管理
# ═══════════════════════════════════════════════════════════════

@auth_bp.route('/credentials', methods=['GET'])
@login_required
def list_credentials():
    """获取当前用户所有已存节点凭据。

    返回: { "credentials": [{"node_name":"...","username":"..."}, ...] }
    """
    user_id = session['user_id']
    creds = db.get_credentials(user_id)
    return {'credentials': creds}, 200


@auth_bp.route('/credentials', methods=['POST'])
@login_required
def save_credential():
    """保存或更新一条节点凭据。

    请求体: { "node_name": "...", "username": "..." }
    """
    data = request.get_json(silent=True) or {}
    node_name = (data.get('node_name') or '').strip()
    username = (data.get('username') or '').strip()

    errors = []
    if not node_name:
        errors.append('节点名称不能为空')
    if not username:
        errors.append('用户名不能为空')
    if errors:
        return {'error': '; '.join(errors)}, 400

    user_id = session['user_id']
    db.save_credential(user_id, node_name, username)
    logger.info('用户 %s 保存节点凭据: %s → %s', user_id, node_name, username)
    return {'message': f'节点 "{node_name}" 凭据已保存'}, 200


@auth_bp.route('/credentials/<node_name>', methods=['DELETE'])
@login_required
def delete_credential(node_name: str):
    """删除一条节点凭据。"""
    user_id = session['user_id']
    ok = db.delete_credential(user_id, node_name)
    if not ok:
        return {'error': '凭据不存在'}, 404
    logger.info('用户 %s 删除节点凭据: %s', user_id, node_name)
    return {'message': f'节点 "{node_name}" 凭据已删除'}, 200
