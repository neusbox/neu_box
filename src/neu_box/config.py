"""Configuration loading and stable runtime path helpers."""

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
