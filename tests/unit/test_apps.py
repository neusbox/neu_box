from __future__ import annotations

import json

from neu_box.database.migrations import migrate_database
from neu_box.master.services.db import (
    Database as MasterDatabase,
    MIGRATIONS_PACKAGE as MASTER_MIGRATIONS,
    REQUIRED_COLUMNS as MASTER_COLUMNS,
    REQUIRED_INDEXES as MASTER_INDEXES,
)
from neu_box.worker.executor.db import (
    Database as WorkerDatabase,
    MIGRATIONS_PACKAGE as WORKER_MIGRATIONS,
    REQUIRED_COLUMNS as WORKER_COLUMNS,
    REQUIRED_INDEXES as WORKER_INDEXES,
)


def test_master_app_health_and_packaged_static(tmp_path, monkeypatch):
    database = tmp_path / "master.db"
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps({"nodes_pool": []}), encoding="utf-8")
    migrate_database(
        database,
        MASTER_MIGRATIONS,
        MASTER_COLUMNS,
        MASTER_INDEXES,
    )
    monkeypatch.setenv("NEU_BOX_DB_PATH", str(database))
    monkeypatch.setenv("NEU_BOX_NODES_CONFIG", str(nodes))
    monkeypatch.setenv("NEU_BOX_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("NEU_BOX_EXPERIMENT_LOG_DIR", str(tmp_path / "exp-logs"))
    MasterDatabase._instance = None

    from neu_box.master.app import create_app

    client = create_app().test_client()
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json["role"] == "master"
    assert health.json["schema_version"] == 1
    assert client.get("/").status_code == 200


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
    assert TaskQueue._instance is None

