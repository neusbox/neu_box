from __future__ import annotations

from neu_box import API_VERSION, __version__
from neu_box.database.migrations import migrate_database
from neu_box.worker.executor.db import (
    Database as WorkerDatabase,
    MIGRATIONS_PACKAGE as WORKER_MIGRATIONS,
    REQUIRED_COLUMNS as WORKER_COLUMNS,
    REQUIRED_INDEXES as WORKER_INDEXES,
)


def test_worker_app_creation_does_not_start_background_threads(tmp_path, monkeypatch):
    database = tmp_path / "worker.db"
    migrate_database(
        database,
        WORKER_MIGRATIONS,
        WORKER_COLUMNS,
        WORKER_INDEXES,
    )
    monkeypatch.setenv("NEU_BOX_DB_PATH", str(database))
    monkeypatch.setenv("NEU_BOX_TASK_LOG_DIR", str(tmp_path / "task-logs"))
    WorkerDatabase._instance = None

    from neu_box.worker.app import create_app
    from neu_box.worker.executor.command import TaskQueue

    TaskQueue._instance = None
    client = create_app().test_client()
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json["role"] == "worker"
    assert health.json["version"] == __version__
    assert health.json["api_version"] == API_VERSION
    assert TaskQueue._instance is None


def test_worker_status_reports_api_version(tmp_path, monkeypatch):
    database = tmp_path / "worker.db"
    migrate_database(
        database,
        WORKER_MIGRATIONS,
        WORKER_COLUMNS,
        WORKER_INDEXES,
    )
    monkeypatch.setenv("NEU_BOX_DB_PATH", str(database))
    monkeypatch.setenv("NEU_BOX_TASK_LOG_DIR", str(tmp_path / "task-logs"))
    WorkerDatabase._instance = None

    from neu_box.worker.app import create_app
    from neu_box.worker.executor.command import TaskQueue

    TaskQueue._instance = None
    client = create_app().test_client()
    status = client.get("/status")
    assert status.status_code == 200
    assert status.json["api_version"] == API_VERSION
    assert status.json["status"] == "online"
