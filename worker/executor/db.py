"""统一数据库模块 — SQLite 持久化，供其他模块使用。

特性:
  - WAL 模式，支持多线程并发
  - 线程本地连接，无需调用方管理连接
  - 自动建表
  - 当前表: tasks（命令执行任务）, sandboxes（沙盒记录）

用法:
    from executor.db import Database
    db = Database.get_instance()

    # tasks
    db.insert_task(task_id, user_id, command, cpu, mem, devices)
    db.update_task_status(task_id, status='running')
    db.update_task_result(task_id, returncode, stdout, stderr, timed_out, error)
    db.get_task(task_id)           → dict | None
    db.get_queue_tasks()           → list[dict]  (queued + running)
    db.get_task_list(limit)        → list[dict]  (recent completed)
    db.cleanup_old_tasks(keep)     → 淘汰旧记录

    # sandboxes
    db.insert_sandbox(name, cpu, mem, devices, cgroup_path, pids)
    db.update_sandbox_pids(name, pids)
    db.delete_sandbox(name)
    db.get_sandbox(name)           → dict | None
    db.list_sandboxes()            → list[dict]
"""

import json
import os
import sqlite3
import threading
import time


class Database:
    """SQLite 数据库单例（线程安全）。"""

    _instance = None

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.getenv('db_dir', './db') + '/neu_box.db'
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self._local = threading.local()
        self._init_tables()

    @classmethod
    def get_instance(cls) -> 'Database':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 内部: 连接管理 ─────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接（线程本地）。"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_tables(self):
        conn = self._get_conn()
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id     TEXT PRIMARY KEY,
                user_id     TEXT    NOT NULL,
                command     TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'queued',
                position    INTEGER DEFAULT 0,
                cpu         INTEGER DEFAULT 0,
                mem         TEXT    DEFAULT '0',
                devices     TEXT    DEFAULT '[]',
                stdout      TEXT,
                stderr      TEXT,
                returncode  INTEGER,
                timed_out   INTEGER DEFAULT 0,
                error       TEXT,
                created_at  REAL,
                started_at  REAL,
                finished_at REAL,
                device_num  INTEGER NOT NULL DEFAULT 0,
                device_ids  TEXT    NOT NULL DEFAULT '[]',
                est_time    INTEGER DEFAULT 0,
                target_spec TEXT    NOT NULL DEFAULT '{"type":"host"}'
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_user   ON tasks(user_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

            CREATE TABLE IF NOT EXISTS sandboxes (
                name        TEXT PRIMARY KEY,
                cpu         INTEGER DEFAULT 0,
                mem         TEXT    DEFAULT '0',
                devices     TEXT    DEFAULT '[]',
                cgroup_path TEXT,
                created_at  REAL,
                pids        TEXT    DEFAULT '[]'
            );
        ''')
        conn.commit()

    # ═══════════════════════════════════════════════════════════
    # Tasks CRUD
    # ═══════════════════════════════════════════════════════════

    # ── 写入 ──────────────────────────────────────────────────

    def insert_task(self, task_id: str, user_id: str, command: str,
                    cpu: int = 0, mem: str = "0", devices: list = None,
                    position: int = 0,
                    device_num: int = 0, device_ids: list = None,
                    target: dict | None = None, est_time: int = 0):
        conn = self._get_conn()
        target = dict(target or {'type': 'host'})
        conn.execute(
            'INSERT INTO tasks (task_id, user_id, command, status, position, '
            'cpu, mem, devices, created_at, device_num, device_ids, '
            'target_spec, est_time) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (task_id, user_id, command, 'queued', position,
             cpu, mem, json.dumps(devices or []), time.time(), device_num,
             json.dumps(device_ids or []),
             json.dumps(target, ensure_ascii=False), est_time))
        conn.commit()

    def update_task_status(self, task_id: str, status: str,
                           started_at: float = None, devices: list = None):
        conn = self._get_conn()
        if devices is not None:
            conn.execute(
                'UPDATE tasks SET status=?, started_at=?, devices=? WHERE task_id=?',
                (status, started_at, json.dumps(devices), task_id))
        elif started_at:
            conn.execute(
                'UPDATE tasks SET status=?, started_at=? WHERE task_id=?',
                (status, started_at, task_id))
        else:
            conn.execute(
                'UPDATE tasks SET status=? WHERE task_id=?',
                (status, task_id))
        conn.commit()

    def update_task_result(self, task_id: str, status: str,
                           returncode: int, stdout: str, stderr: str,
                           timed_out: bool = False, error: str = None,
                           finished_at: float = None):
        conn = self._get_conn()
        conn.execute(
            'UPDATE tasks SET status=?, returncode=?, stdout=?, stderr=?, '
            'timed_out=?, error=?, finished_at=? WHERE task_id=?',
            (status, returncode, stdout, stderr,
             1 if timed_out else 0, error,
             finished_at or time.time(), task_id))
        conn.commit()

    def update_position_batch(self, task_ids: list[str]):
        """批量更新 position（一次性更新所有排队任务的位置）。"""
        conn = self._get_conn()
        for i, tid in enumerate(task_ids):
            conn.execute(
                'UPDATE tasks SET position=? WHERE task_id=?',
                (i + 1, tid))
        conn.commit()

    # ── 查询 ──────────────────────────────────────────────────

    def get_task(self, task_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            'SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def get_queue_tasks(self) -> list[dict]:
        """返回所有 queued + running 任务（按 position / 时间排序）。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('queued','running') "
            "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, "
            "position ASC, created_at ASC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_recent_tasks(self, limit: int = 100) -> list[dict]:
        """返回最近完成的任务列表。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('completed','failed') "
            "ORDER BY finished_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── 清理 ──────────────────────────────────────────────────

    def delete_task(self, task_id: str):
        conn = self._get_conn()
        conn.execute('DELETE FROM tasks WHERE task_id=?', (task_id,))
        conn.commit()

    def cleanup_old_tasks(self, keep: int = 200):
        """保留最近 keep 条已完成任务，超出部分淘汰。"""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM tasks WHERE task_id IN ("
            "  SELECT task_id FROM tasks "
            "  WHERE status IN ('completed','failed') "
            "  ORDER BY finished_at DESC "
            "  LIMIT -1 OFFSET ?"
            ")", (keep,))
        conn.commit()

    # ═══════════════════════════════════════════════════════════
    # Sandboxes CRUD
    # ═══════════════════════════════════════════════════════════

    def insert_sandbox(self, name: str, cpu: int = 0, mem: str = "0",
                       devices: list = None, cgroup_path: str = "",
                       pids: list = None):
        conn = self._get_conn()
        conn.execute(
            'INSERT OR REPLACE INTO sandboxes '
            '(name, cpu, mem, devices, cgroup_path, created_at, pids) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (name, cpu, mem, json.dumps(devices or []), cgroup_path,
             time.time(), json.dumps(pids or [])))
        conn.commit()

    def update_sandbox_pids(self, name: str, pids: list):
        conn = self._get_conn()
        conn.execute(
            'UPDATE sandboxes SET pids=? WHERE name=?',
            (json.dumps(pids), name))
        conn.commit()

    def delete_sandbox(self, name: str):
        conn = self._get_conn()
        conn.execute('DELETE FROM sandboxes WHERE name=?', (name,))
        conn.commit()

    def get_sandbox(self, name: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            'SELECT * FROM sandboxes WHERE name=?', (name,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_sandboxes(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute('SELECT * FROM sandboxes').fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ═══════════════════════════════════════════════════════════
    # 通用
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        # 将 JSON 字符串字段解析回 Python 对象
        for key in ('devices', 'pids', 'device_ids'):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        raw = d.get('target_spec')
        if isinstance(raw, str):
            try:
                d['target_spec'] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d['target_spec'] = {}
        return d
