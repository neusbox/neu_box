from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

from neu_box.database.migrations import (
    MigrationError,
    backup_database,
    check_database,
    migrate_database,
    require_current_schema,
    schema_status,
)
from neu_box.master.services.db import (
    MIGRATIONS_PACKAGE as MASTER_MIGRATIONS,
    REQUIRED_COLUMNS as MASTER_COLUMNS,
    REQUIRED_INDEXES as MASTER_INDEXES,
)
from neu_box.worker.executor.db import (
    MIGRATIONS_PACKAGE as WORKER_MIGRATIONS,
    REQUIRED_COLUMNS as WORKER_COLUMNS,
    REQUIRED_INDEXES as WORKER_INDEXES,
)


@pytest.mark.parametrize(
    ("package", "columns", "indexes"),
    [
        (MASTER_MIGRATIONS, MASTER_COLUMNS, MASTER_INDEXES),
        (WORKER_MIGRATIONS, WORKER_COLUMNS, WORKER_INDEXES),
    ],
)
def test_fresh_database_migrates_to_current(tmp_path, package, columns, indexes):
    database = tmp_path / "fresh.db"

    before = schema_status(database, package)
    assert before.state == "missing"
    assert before.pending == (1,)

    after = migrate_database(database, package, columns, indexes)

    assert after.state == "current"
    assert after.current == 1
    require_current_schema(database, package, columns, indexes)
    checked = check_database(database, package, columns, indexes)
    assert checked.state == "current"

    with sqlite3.connect(database) as conn:
        row = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchone()
    assert row[0:2] == (1, "initial")
    assert row[2].startswith("sha256:")


def test_known_existing_worker_database_is_baselined_without_data_loss(tmp_path):
    database = tmp_path / "worker.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                command TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                position INTEGER DEFAULT 0, cpu INTEGER DEFAULT 0,
                mem TEXT DEFAULT '0', devices TEXT DEFAULT '[]', stdout TEXT,
                stderr TEXT, returncode INTEGER, timed_out INTEGER DEFAULT 0,
                error TEXT, created_at REAL, started_at REAL, finished_at REAL,
                device_num INTEGER NOT NULL DEFAULT 0,
                device_ids TEXT NOT NULL DEFAULT '[]', est_time INTEGER DEFAULT 0,
                target_spec TEXT NOT NULL DEFAULT '{"type":"host"}',
                harmless_legacy_column TEXT
            );
            CREATE INDEX idx_tasks_user ON tasks(user_id);
            CREATE INDEX idx_tasks_status ON tasks(status);
            CREATE INDEX idx_tasks_created ON tasks(created_at);
            CREATE TABLE sandboxes (
                name TEXT PRIMARY KEY, cpu INTEGER DEFAULT 0,
                mem TEXT DEFAULT '0', devices TEXT DEFAULT '[]',
                cgroup_path TEXT, created_at REAL, pids TEXT DEFAULT '[]'
            );
            INSERT INTO tasks (task_id, user_id, command, status)
            VALUES ('keep-me', 'lab-user', 'true', 'completed');
        """)

    status = migrate_database(
        database,
        WORKER_MIGRATIONS,
        WORKER_COLUMNS,
        WORKER_INDEXES,
    )

    assert status.state == "current"
    with sqlite3.connect(database) as conn:
        task = conn.execute(
            "SELECT user_id, command FROM tasks WHERE task_id='keep-me'"
        ).fetchone()
    assert task == ("lab-user", "true")


def test_unknown_existing_database_is_refused_without_mutation(tmp_path):
    database = tmp_path / "unknown.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")

    with pytest.raises(MigrationError, match="缺少"):
        migrate_database(
            database,
            WORKER_MIGRATIONS,
            WORKER_COLUMNS,
            WORKER_INDEXES,
        )

    with sqlite3.connect(database) as conn:
        history = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
    assert history is None


def test_empty_migration_history_cannot_bypass_schema_adoption(tmp_path):
    database = tmp_path / "fake-tracked.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE unrelated (value TEXT);
        """)

    with pytest.raises(MigrationError, match="没有任何记录"):
        migrate_database(
            database,
            WORKER_MIGRATIONS,
            WORKER_COLUMNS,
            WORKER_INDEXES,
        )


def test_current_history_does_not_hide_broken_schema(tmp_path):
    database = tmp_path / "broken-current.db"
    migrate_database(
        database,
        WORKER_MIGRATIONS,
        WORKER_COLUMNS,
        WORKER_INDEXES,
    )
    with sqlite3.connect(database) as conn:
        conn.execute("DROP INDEX idx_tasks_status")
        conn.commit()

    with pytest.raises(MigrationError, match="缺少索引"):
        require_current_schema(
            database,
            WORKER_MIGRATIONS,
            WORKER_COLUMNS,
            WORKER_INDEXES,
        )


def _temporary_migration_package(tmp_path: Path, files: dict[str, str]) -> str:
    package_name = f"migration_fixture_{tmp_path.name.replace('-', '_')}"
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, content in files.items():
        (package / name).write_text(content, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    return package_name


def test_failed_migration_rolls_back_only_that_version(tmp_path):
    package = _temporary_migration_package(tmp_path, {
        "0001_initial.sql": "CREATE TABLE records (id INTEGER PRIMARY KEY);",
        "0002_broken.sql": (
            "ALTER TABLE records ADD COLUMN value TEXT;\n"
            "INSERT INTO table_that_does_not_exist VALUES (1);"
        ),
    })
    database = tmp_path / "broken.db"

    with pytest.raises(sqlite3.OperationalError):
        migrate_database(database, package, {}, ())

    with sqlite3.connect(database) as conn:
        versions = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(records)")
        }
    assert versions == [(1,)]
    assert columns == {"id"}


def test_python_migration_cannot_commit_runner_transaction(tmp_path):
    package = _temporary_migration_package(tmp_path, {
        "0001_initial.py": (
            "def upgrade(conn):\n"
            "    conn.execute('CREATE TABLE escaped (value TEXT)')\n"
            "    conn.commit()\n"
        ),
    })
    database = tmp_path / "illegal-commit.db"

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        migrate_database(database, package, {}, ())

    with sqlite3.connect(database) as conn:
        escaped = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='escaped'"
        ).fetchone()
        versions = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    assert escaped is None
    assert versions == []


def test_applied_migration_checksum_drift_is_rejected(tmp_path):
    package = _temporary_migration_package(tmp_path, {
        "0001_initial.sql": "CREATE TABLE records (id INTEGER PRIMARY KEY);",
    })
    database = tmp_path / "drift.db"
    migrate_database(database, package, {}, ())

    package_path = tmp_path / package
    (package_path / "0001_initial.sql").write_text(
        "CREATE TABLE records (id INTEGER PRIMARY KEY, changed TEXT);",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="checksum"):
        schema_status(database, package)


def test_backup_uses_consistent_sqlite_copy(tmp_path):
    database = tmp_path / "source.db"
    migrate_database(
        database,
        WORKER_MIGRATIONS,
        WORKER_COLUMNS,
        WORKER_INDEXES,
    )
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, user_id, command, status) "
            "VALUES ('one', 'user', 'echo ok', 'completed')"
        )
        conn.commit()

    backup = backup_database(database, tmp_path / "backups", "worker")

    assert backup.is_file()
    assert check_database(
        backup,
        WORKER_MIGRATIONS,
        WORKER_COLUMNS,
        WORKER_INDEXES,
    ).state == "current"
    with sqlite3.connect(backup) as conn:
        count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert count == 1
