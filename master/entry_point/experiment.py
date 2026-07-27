"""实验管理 API — 块（block）式实验笔记。

实验由 blocks 数组组成，按顺序渲染:
  text block:  {"type":"text","content":"..."}
  task block:  {"type":"task","task_id":"...","node_id":"...",
                "command":"...","log":{...}}
"""

import logging
import os
import uuid
from datetime import datetime

from flask import Blueprint, request

from src_manager.db import Database
from entry_point.auth import login_required

experiment_bp = Blueprint('experiment', __name__)
db = Database.get_instance()
logger = logging.getLogger('master.experiment')

# 实验日志缓存目录
EXP_LOG_DIR = os.getenv('EXP_LOG_DIR', os.path.join(os.path.dirname(__file__), '..', 'logs', 'exp'))
os.makedirs(EXP_LOG_DIR, exist_ok=True)

# 图片上传大小限制 (默认 10MB)
UPLOAD_MAX_SIZE = int(os.getenv('upload_max_size', str(10 * 1024 * 1024)))
# 允许的图片 MIME 类型
ALLOWED_MIMETYPES = {
    'image/png', 'image/jpeg', 'image/gif',
    'image/webp', 'image/bmp', 'image/svg+xml',
}


@experiment_bp.route('/', methods=['POST'])
@login_required
def create_experiment():
    """创建实验。

    请求体:
      { "title": "...", "blocks": [...], "tags": [...], "created_by": "...",
        "folder_id": "..." }
    """
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    if not title:
        return {'error': '实验标题不能为空'}, 400
    exp_id = db.create_experiment(
        title=title,
        blocks=data.get('blocks') or [],
        tags=data.get('tags') or [],
        created_by=(data.get('created_by') or '').strip(),
        folder_id=data.get('folder_id') or None,
    )
    _save_logs(data.get('logs') or {})
    return {'id': exp_id, 'message': '实验记录已创建'}, 201


@experiment_bp.route('/', methods=['GET'])
@login_required
def list_experiments():
    search = (request.args.get('search') or '').strip()
    tag = (request.args.get('tag') or '').strip()
    created_by = (request.args.get('created_by') or '').strip()
    folder_id = request.args.get('folder_id') or None
    limit = request.args.get('limit', 100, type=int)
    experiments = db.list_experiments(search=search, tag=tag,
                                      created_by=created_by,
                                      folder_id=folder_id,
                                      limit=limit)
    return {'experiments': experiments, 'total': len(experiments)}, 200


@experiment_bp.route('/<exp_id>', methods=['GET'])
@login_required
def get_experiment(exp_id: str):
    exp = db.get_experiment(exp_id)
    if not exp:
        return {'error': '实验记录不存在'}, 404
    return exp, 200


@experiment_bp.route('/<exp_id>', methods=['PUT'])
@login_required
def update_experiment(exp_id: str):
    """全量更新实验（含 blocks）。"""
    exp = db.get_experiment(exp_id)
    if not exp:
        return {'error': '实验记录不存在'}, 404
    data = request.get_json(silent=True) or {}
    db.update_experiment(
        exp_id,
        title=data.get('title'),
        blocks=data.get('blocks'),
        tags=data.get('tags'),
        folder_id=data.get('folder_id'),
    )
    _save_logs(data.get('logs') or {})
    return {'message': '已保存'}, 200


@experiment_bp.route('/upload-image', methods=['POST'])
@login_required
def upload_image():
    """上传图片，返回 Markdown 可用的 URL。

    请求: multipart/form-data, 字段名 file
    返回: { "url": "/static/uploads/2026/06/abc123.png", "size": 12345 }
    """
    if 'file' not in request.files:
        return {'error': '缺少 file 字段'}, 400

    file = request.files['file']
    if not file.filename:
        return {'error': '文件名为空'}, 400

    # 验证 MIME 类型
    mimetype = (file.content_type or '').lower()
    if mimetype not in ALLOWED_MIMETYPES:
        return {'error': f'不支持的图片类型: {mimetype}'}, 400

    # 大小检查
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > UPLOAD_MAX_SIZE:
        max_mb = UPLOAD_MAX_SIZE // (1024 * 1024)
        return {'error': f'图片过大 (最大 {max_mb}MB)'}, 400

    # 扩展名
    ext = os.path.splitext(file.filename)[1].lower()
    if not ext or len(ext) > 10:
        ext = '.png'
    if ext == '.jpg':
        ext = '.jpeg'

    # 按月份分目录，UUID 命名
    month_dir = datetime.now().strftime('%Y/%m')
    upload_dir = os.path.join(
        os.path.dirname(__file__), '..', 'static', 'uploads', month_dir
    )
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    url = f"/static/uploads/{month_dir}/{filename}"
    logger.warning("图片上传成功: %s (%s bytes)", url, size)

    return {'url': url, 'size': size}, 201


# ═══════════════════════════════════════════════════════════════
# Folders API
# ═══════════════════════════════════════════════════════════════

@experiment_bp.route('/folders', methods=['GET'])
@login_required
def list_folders():
    """获取文件夹树。"""
    tree = db.get_folder_tree()
    return {'folders': tree}, 200


@experiment_bp.route('/folders', methods=['POST'])
@login_required
def create_folder():
    """创建文件夹。

    请求体: { "name": "...", "parent_id": "..." }
    """
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return {'error': '文件夹名称不能为空'}, 400
    fid = db.create_folder(name=name, parent_id=data.get('parent_id') or None)
    return {'id': fid, 'name': name}, 201


@experiment_bp.route('/folders/<fid>', methods=['PUT'])
@login_required
def update_folder(fid: str):
    """重命名或移动文件夹。

    请求体: { "name": "...", "parent_id": "..." }
    """
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if name:
        db.rename_folder(fid, name)
    if 'parent_id' in data:
        ok = db.move_folder(fid, data.get('parent_id') or None)
        if not ok:
            return {'error': '不能移动到自己的子文件夹中'}, 400
    return {'message': '已更新'}, 200


@experiment_bp.route('/folders/<fid>', methods=['DELETE'])
@login_required
def delete_folder(fid: str):
    if not db.delete_folder(fid):
        return {'error': '文件夹不存在'}, 404
    return {'message': '已删除'}, 200


@experiment_bp.route('/<exp_id>', methods=['DELETE'])
@login_required
def delete_experiment(exp_id: str):
    # 清理实验引用的图片
    exp = db.get_experiment(exp_id)
    if exp:
        _cleanup_images(exp.get('blocks', []))
    if not db.delete_experiment(exp_id):
        return {'error': '实验记录不存在'}, 404
    return {'message': '已删除'}, 200


def _cleanup_images(blocks: list):
    """扫描 blocks 中的 /static/uploads/ 图片，删除未被其他实验引用的文件。"""
    import re
    urls = set()
    for block in blocks:
        if block.get('type') != 'text':
            continue
        content = block.get('content', '') or ''
        for m in re.finditer(r'/static/uploads/(\d{4}/\d{2}/[a-f0-9]+\.\w+)', content):
            urls.add(m.group(1))

    if not urls:
        return

    # 获取所有其他实验的内容，检查是否仍被引用
    all_exps = db.list_experiments(limit=10000)
    referenced = set()
    for other in all_exps:
        for b in (other.get('blocks') or []):
            if b.get('type') != 'text':
                continue
            content = b.get('content', '') or ''
            for m in re.finditer(r'/static/uploads/(\d{4}/\d{2}/[a-f0-9]+\.\w+)', content):
                referenced.add(m.group(1))

    uploads_root = os.path.join(
        os.path.dirname(__file__), '..', 'static', 'uploads'
    )
    for rel_path in urls:
        if rel_path not in referenced:
            fp = os.path.join(uploads_root, rel_path)
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
                    logger.warning("清理孤儿图片: %s", rel_path)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════
# 日志缓存
# ═══════════════════════════════════════════════════════════════

def _save_logs(logs: dict):
    """将前端传来的日志副本保存到 EXP_LOG_DIR。logs: { task_id: "log text", ... }"""
    for task_id, text in logs.items():
        if not task_id or not text:
            continue
        try:
            path = os.path.join(EXP_LOG_DIR, f'{task_id}.log')
            with open(path, 'w') as f:
                f.write(text)
        except Exception as e:
            logger.warning("保存实验日志失败 %s: %s", task_id, e)


@experiment_bp.route('/log/<task_id>', methods=['GET'])
@login_required
def serve_exp_log(task_id: str):
    """提供缓存的实验日志（副本，不受 worker 任务删除影响）。"""
    path = os.path.join(EXP_LOG_DIR, f'{task_id}.log')
    if not os.path.isfile(path):
        return '(日志不存在或已被清理)', 404
    try:
        with open(path, 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/plain; charset=utf-8'}
    except Exception as e:
        return f'(读取日志失败: {e})', 500
