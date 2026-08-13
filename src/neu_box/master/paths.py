"""Master persistent-data and packaged-resource paths."""

from __future__ import annotations

from pathlib import Path

from neu_box.config import configured_path, user_config_dir, user_data_dir


def static_dir() -> Path:
    return (Path(__file__).resolve().parent / "static").resolve()


def nodes_config_path() -> Path:
    return configured_path(
        "NEU_BOX_NODES_CONFIG",
        user_config_dir() / "nodes.json",
    )


def uploads_dir() -> Path:
    return configured_path(
        "NEU_BOX_UPLOAD_DIR",
        user_data_dir("master") / "uploads",
    )


def experiment_logs_dir() -> Path:
    return configured_path(
        "NEU_BOX_EXPERIMENT_LOG_DIR",
        user_data_dir("master") / "experiment-logs",
        legacy="EXP_LOG_DIR",
    )

