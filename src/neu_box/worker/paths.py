"""Worker persistent-data and runtime executable paths."""

from __future__ import annotations

from pathlib import Path

from neu_box.config import configured_path, user_data_dir


_DEFAULT_SANDBOX_EXECUTABLE = Path(
    "/usr/libexec/neu-box/neu-box-sandbox"
)


def sandbox_executable_path() -> Path:
    """Return the configured native sandbox CLI path."""
    return configured_path(
        "NEU_BOX_SANDBOX_EXECUTABLE",
        _DEFAULT_SANDBOX_EXECUTABLE,
    )


def task_logs_dir() -> Path:
    return configured_path(
        "NEU_BOX_TASK_LOG_DIR",
        user_data_dir("worker") / "task-logs",
    )
