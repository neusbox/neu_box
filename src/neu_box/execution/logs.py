"""任务输出日志：写、读、路径规则。

host 和 docker 两种执行后端都往同一个文件里追加任务的 stdout+stderr；
HTTP 侧按 offset 增量拉，或者按 tail 拿末尾一段。路径规则、写入的加锁和
flush、读取的边界处理都收在这里 —— 这三段原本散在三个文件里各写一份。
"""

from __future__ import annotations

import logging
import os
import string
import threading

from neu_box.config import task_logs_dir

logger = logging.getLogger(__name__)

# 模块级变量，读的时候实时取（不在导入时绑定到别处），这样运行时改它能同时
# 影响写入侧和读取侧 —— 一处生效，不会出现"写在新目录、读在旧目录"。
LOG_DIR = str(task_logs_dir())


def log_path(task_id: str) -> str:
    """任务输出日志的路径。"""
    return os.path.join(LOG_DIR, f'{task_id}.log')


def task_id_of(sandbox_name: str) -> str:
    """从沙盒名反推 task_id。

    Worker 生成的 ID 是最后一段；用户名可能带下划线，所以不能从左往右拆
    —— 拆错会把日志写到别的文件名下，``GET /tasks/<id>/log`` 就找不到了。
    """
    stem = sandbox_name[:-6] if sandbox_name.endswith('.slice') else sandbox_name
    payload = stem[4:] if stem.startswith('sbx_') else stem
    owner, separator, task_id = payload.rpartition('_')
    # 队列生成的 ID 是 12 位十六进制；其余（手工建的、历史沙盒名）退回从
    # 左拆的老办法 —— 它们的 ID 允许带下划线。
    if (separator and owner and len(task_id) == 12
            and all(char in string.hexdigits for char in task_id)):
        return task_id
    parts = stem.split('_', 2)
    return parts[2] if len(parts) == 3 else stem


class TaskLog:
    """往任务日志文件追加输出：线程安全，每条 flush（要能实时看）。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, chunk: bytes | str | None) -> None:
        if not chunk:
            return
        if isinstance(chunk, bytes):
            text = chunk.decode('utf-8', errors='replace')
        else:
            text = str(chunk)
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()


def remove(task_id: str) -> None:
    """删掉任务日志（任务记录被删除时）。"""
    path = log_path(task_id)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        logger.warning('删除日志文件失败: %s', path)


def read_log(task_id: str, offset: int = 0, limit: int = 0,
             tail: int = 0) -> dict:
    """增量读取：按 offset + limit，或按 tail 取末尾一段。

    返回 ``{data, offset, total_size}``；读失败时多一个 ``error`` 键。
    ``total_size`` 是文件总长 —— 客户端拿它和 offset 算出下一次从哪继续。
    """
    path = log_path(task_id)
    if not os.path.isfile(path):
        return {'data': '', 'offset': 0, 'total_size': 0}
    size = os.path.getsize(path)
    if tail > 0:
        offset, limit = max(0, size - tail), min(tail, size)
    elif not limit and not offset:
        limit = size
    offset = max(0, min(offset, size))
    limit = max(1, min(limit, size - offset))
    try:
        with open(path, 'rb') as stream:
            stream.seek(offset)
            data = stream.read(limit).decode('utf-8', errors='replace')
    except OSError as exc:
        return {'data': '', 'offset': 0, 'total_size': size, 'error': str(exc)}
    return {'data': data, 'offset': offset, 'total_size': size}
