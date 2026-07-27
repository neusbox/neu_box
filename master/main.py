import json
import logging
import os
import secrets
import time
from datetime import timedelta
from logging.handlers import RotatingFileHandler

import flask
from dotenv import load_dotenv

load_dotenv()

# ── 集中日志配置 ─────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(_log_dir, exist_ok=True)

# 解析日志级别（兼容 .env 中带引号的写法）
_raw_level = os.getenv('LOG_LEVEL', 'INFO').strip().strip('"').strip("'").upper()
_log_level = getattr(logging, _raw_level, logging.INFO)

_log_fmt = logging.Formatter(
    '%(asctime)s [%(name)s] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

# 按启动时间命名日志文件，每次重启写新文件
_start_ts = time.strftime('%Y%m%d-%H%M%S')
_log_file = os.path.join(_log_dir, f'master-{_start_ts}.log')

_file_handler = RotatingFileHandler(
    _log_file, maxBytes=10 * 1024 * 1024, backupCount=5,
)
_file_handler.setFormatter(_log_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_fmt)

# 文件、控制台、root logger 统一使用 LOG_LEVEL
_file_handler.setLevel(_log_level)
_console_handler.setLevel(_log_level)
_root_logger = logging.getLogger()
_root_logger.setLevel(_log_level)
_root_logger.addHandler(_file_handler)
_root_logger.addHandler(_console_handler)

logging.getLogger('master').info('Master 启动，日志级别=%s，日志文件=%s', _raw_level, _log_file)


from entry_point.terminal import terminal_bp
from entry_point.command import command_bp
from entry_point.nodes import nodes_bp
from entry_point.experiment import experiment_bp
from entry_point.auth import auth_bp
from src_manager.nodes_pool import Nodes_Pool
from src_manager.db import Database

app = flask.Flask(__name__, static_folder='static', static_url_path='/static')

# ── Session 配置 ─────────────────────────────────────────────
_raw_secret = os.getenv('SECRET_KEY', '').strip().strip('"').strip("'")
app.secret_key = _raw_secret if _raw_secret else secrets.token_hex(32)
if not _raw_secret:
    logging.getLogger('master').warning(
        '未设置 SECRET_KEY 环境变量，使用随机密钥（重启后所有用户需重新登录）')

# Session 持久化：cookie 保存 7 天，浏览器关闭不清除
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

app.register_blueprint(auth_bp, url_prefix='/auth')
app.register_blueprint(terminal_bp, url_prefix='/terminal')
app.register_blueprint(command_bp, url_prefix='/command')
app.register_blueprint(nodes_bp, url_prefix='/nodes')
app.register_blueprint(experiment_bp, url_prefix='/experiments')


# ── 初始化管理员 ─────────────────────────────────────────────
def _init_admin():
    """从环境变量或默认值创建管理员账号。"""
    db = Database.get_instance()
    admin_user = os.getenv('ADMIN_USER', 'admin').strip().strip('"').strip("'")
    admin_pass = os.getenv('ADMIN_PASS', 'admin').strip().strip('"').strip("'")

    existing = db.get_user(admin_user)
    if existing:
        logging.getLogger('master').info('管理员账号已存在: %s', admin_user)
        return

    uid = db.create_user(admin_user, admin_pass, role='admin')
    if uid:
        logging.getLogger('master').info(
            '已创建管理员: %s (初始密码: %s)', admin_user,
            '***已通过 ADMIN_PASS 设置***' if os.getenv('ADMIN_PASS') else admin_pass)
    else:
        logging.getLogger('master').warning('创建管理员失败（可能已存在）')


@app.route('/')
def home():
    return flask.send_from_directory('static', 'index.html')


if __name__ == '__main__':
    ip = os.getenv('listen', '0.0.0.0')
    port = int(os.getenv('port', '25565'))

    _init_admin()

    # 启动后台轮询，定期查询所有 worker 节点状态
    poll_interval = int(os.getenv('poll_interval', '15'))
    Nodes_Pool.get_nodes_pool().start_polling(interval=poll_interval)

    app.run(host=ip, port=port)