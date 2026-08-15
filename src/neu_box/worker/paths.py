"""Worker persistent-data and packaged-resource paths."""

from __future__ import annotations

from pathlib import Path

from neu_box.config import configured_path, user_data_dir


def resources_dir() -> Path:
    return (Path(__file__).resolve().parent / "resources").resolve()


def sandbox_script_path() -> Path:
    return configured_path(
        "NEU_BOX_SANDBOX_SCRIPT",
        resources_dir() / "sandbox" / "v2" / "sandbox.sh",
        legacy="sandbox_script_path",
    )


def task_logs_dir() -> Path:
    return configured_path(
        "NEU_BOX_TASK_LOG_DIR",
        user_data_dir("worker") / "task-logs",
        legacy="LOG_DIR",
    )

