"""环境变量读取 + 运行时路径推导。

两者放一起：路径都是"环境变量给就听、不给就按 XDG 约定算"，分开两个文件
只会让"这个路径谁决定的"要在两个地方找。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Configuration cannot be loaded safely."""


def load_role_environment(
    role: str,
    explicit_path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Load one explicit role environment file without cwd discovery."""
    raw_path = explicit_path or os.getenv("NEU_BOX_CONFIG", "").strip()
    if raw_path:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"配置文件不存在: {path}")
        load_dotenv(path, override=False)
        return path

    system_path = Path(f"/etc/neu-box/{role}.env")
    if system_path.is_file():
        load_dotenv(system_path, override=False)
        return system_path
    return None


def env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        value = default
    return value.strip().strip('"').strip("'")


def env_int(name: str, default: int) -> int:
    value = env_text(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，实际为 {value!r}") from exc


def user_data_dir(role: str) -> Path:
    root = os.getenv("XDG_DATA_HOME", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".local" / "share"
    return (base / "neu-box" / role).resolve()


def user_config_dir() -> Path:
    root = os.getenv("XDG_CONFIG_HOME", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".config"
    return (base / "neu-box").resolve()


def user_log_dir() -> Path:
    root = os.getenv("XDG_STATE_HOME", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state"
    return (base / "neu-box" / "logs").resolve()


def configured_path(
    name: str,
    default: Path,
) -> Path:
    value = env_text(name)
    return Path(value).expanduser().resolve() if value else default.resolve()


# ── Worker 运行时路径 ───────────────────────────────────────────────

_DEFAULT_SANDBOX_EXECUTABLE = Path("/usr/libexec/neu-box/neu-box-sandbox")


def sandbox_executable_path() -> Path:
    """native sandbox CLI（neu-box-sandbox）的路径。"""
    return configured_path(
        "NEU_BOX_SANDBOX_EXECUTABLE",
        _DEFAULT_SANDBOX_EXECUTABLE,
    )


def task_logs_dir() -> Path:
    """任务输出日志目录。"""
    return configured_path(
        "NEU_BOX_TASK_LOG_DIR",
        user_data_dir("worker") / "task-logs",
    )
