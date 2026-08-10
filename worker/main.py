# author:peng yutao 彭玉涛
# date: 2026.7.27
# TIP: This project is only used in a small group.


import logging
import os
import time
from logging.handlers import RotatingFileHandler

import flask
from dotenv import load_dotenv

load_dotenv()

# ── 自动安装 neu-sbox.sh 到 /etc/profile.d/ ──────────────────
# 用 sudo 启动 Worker 时自动完成，无需额外操作
_script_dir = os.path.dirname(os.path.abspath(__file__))
_sbox_src = os.path.join(_script_dir, 'scripts', 'client', 'neu-sbox.sh')
_sbox_dst = '/usr/local/bin/neu-sbox'
if os.path.isfile(_sbox_src):
    try:
        import shutil
        shutil.copy2(_sbox_src, _sbox_dst)
        os.chmod(_sbox_dst, 0o755)
        print(f'[init] neu-sbox 已安装到 {_sbox_dst}')
    except PermissionError:
        print(f'[init] 警告: 无法写入 {_sbox_dst}，请用 sudo 启动或手动安装')
    except Exception as e:
        print(f'[init] 警告: 安装 neu-sbox 失败: {e}')

# ── 集中日志配置 ─────────────────────────────────────────────
_log_dir = os.getenv('task_log_dir', os.path.join(os.path.dirname(__file__), 'logs'))
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
_log_file = os.path.join(_log_dir, f'worker-{_start_ts}.log')

_file_handler = RotatingFileHandler(
    _log_file, maxBytes=10 * 1024 * 1024, backupCount=5,
)
_file_handler.setFormatter(_log_fmt)

# 控制台输出
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_fmt)

# 文件、控制台、root logger 统一使用 LOG_LEVEL
_file_handler.setLevel(_log_level)
_console_handler.setLevel(_log_level)
_root_logger = logging.getLogger()
_root_logger.setLevel(_log_level)
_root_logger.addHandler(_file_handler)
_root_logger.addHandler(_console_handler)

# 用 logger 而非 print，确保写入日志文件
logging.getLogger('worker').info('Worker 启动，日志级别=%s，日志文件=%s', _raw_level, _log_file)

from executor.status import status_bp
from executor.command import command_bp
from executor.sandbox_api import sandbox_bp
from executor.sbx_manager import SbxManager

app = flask.Flask(__name__)

app.register_blueprint(command_bp, url_prefix='/command')
app.register_blueprint(sandbox_bp, url_prefix='/sandbox')
# /status 直接挂载到根路径，方便 master 查询
app.register_blueprint(status_bp)

# 启动定时收尸线程（清理过期/孤儿沙盒）
SbxManager.get_instance().start_reaper()


@app.route('/')
def home():
    return flask.send_from_directory('static', 'index.html')


if __name__ == '__main__':
    app.run(host=os.getenv('listen'), port=int(os.getenv('port')))
