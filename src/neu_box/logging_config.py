"""Shared console and rotating-file logging configuration."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from neu_box.config import env_text, user_log_dir


def configure_logging(role: str) -> Path:
    log_dir_raw = env_text("NEU_BOX_LOG_DIR")
    log_dir = Path(log_dir_raw).expanduser().resolve() if log_dir_raw else user_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    raw_level = env_text("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, raw_level, None)
    if not isinstance(level, int):
        raise ValueError(f"无效的 LOG_LEVEL: {raw_level}")

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log_file = log_dir / f"{role}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    logging.getLogger(role).info(
        "%s 启动，日志级别=%s，日志文件=%s",
        role.capitalize(),
        raw_level,
        log_file,
    )
    return log_file

