#!/usr/bin/env python3
"""Install, upgrade, inspect, or roll back a Neu Box release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pwd
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROLES = ("worker",)
LEGACY_SPLIT_ROLES = ("master",)
_LEGACY_ROLES_STATE_KEY = "_legacy_split_roles"
STATE_FORMAT = 1
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class InstallError(RuntimeError):
    """A deployment cannot be completed safely."""


@dataclass(frozen=True)
class Layout:
    root: Path

    def path(self, absolute: str) -> Path:
        path = Path(absolute)
        if not path.is_absolute():
            raise ValueError(f"expected absolute path: {absolute}")
        if self.root == Path("/"):
            return path.resolve()
        relative = path.relative_to("/")
        if ".." in relative.parts:
            raise ValueError(f"path escapes installation root: {absolute}")
        mapped = (self.root / relative).resolve()
        try:
            mapped.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(f"path escapes installation root: {absolute}") from exc
        return mapped

    @property
    def opt(self) -> Path:
        return self.path("/opt/neu-box")

    @property
    def releases(self) -> Path:
        return self.opt / "releases"

    @property
    def current(self) -> Path:
        return self.opt / "current"

    @property
    def config(self) -> Path:
        return self.path("/etc/neu-box")

    @property
    def data(self) -> Path:
        return self.path("/var/lib/neu-box")

    @property
    def logs(self) -> Path:
        return self.path("/var/log/neu-box")

    @property
    def backups(self) -> Path:
        return self.path("/var/backups/neu-box")

    @property
    def units(self) -> Path:
        return self.path("/etc/systemd/system")

    @property
    def sbin(self) -> Path:
        return self.path("/usr/local/sbin")

    @property
    def bin(self) -> Path:
        return self.path("/usr/local/bin")

    @property
    def state_file(self) -> Path:
        return self.data / "install-state.json"


@dataclass(frozen=True)
class PathSnapshot:
    """Recoverable state of one file or symlink before deployment."""

    path: Path
    kind: str
    backup: Path | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class ServiceState:
    active: bool
    enabled: bool


def _log(message: str) -> None:
    print(f"[neu-box] {message}", flush=True)


def _run(
    command: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    _log("执行: " + " ".join(command))
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
        env=env,
    )


def _architecture() -> str:
    aliases = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    machine = platform.machine().lower()
    return aliases.get(machine, machine)


def _valid_version(value: object) -> bool:
    return isinstance(value, str) and VERSION_RE.fullmatch(value) is not None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def verify_release(source: Path) -> dict:
    source = source.resolve()
    manifest_path = source / "manifest.json"
    checksum_path = source / "SHA256SUMS"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise InstallError(f"不是 Neu Box 发布目录: {source}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"无法读取 manifest.json: {exc}") from exc
    if manifest.get("format") != 1 or manifest.get("name") != "neu-box":
        raise InstallError("不支持的发布包 manifest 格式")
    raw_version = manifest.get("version")
    if not _valid_version(raw_version):
        raise InstallError(f"无效发布版本: {raw_version!r}")
    version = str(raw_version)
    if manifest.get("os") != "linux":
        raise InstallError("发布包不是 Linux 版本")
    if manifest.get("architecture") != _architecture():
        raise InstallError(
            f"架构不匹配: 包={manifest.get('architecture')} 本机={_architecture()}"
        )

    expected: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        checksum_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw_line.strip():
            continue
        try:
            checksum, relative = raw_line.split("  ", 1)
        except ValueError as exc:
            raise InstallError(f"SHA256SUMS 第 {line_number} 行格式错误") from exc
        relative_path = Path(relative)
        if (
            len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)
            or relative_path.is_absolute()
            or relative_path.as_posix() in {"", ".", "SHA256SUMS"}
            or ".." in relative_path.parts
        ):
            raise InstallError(f"SHA256SUMS 第 {line_number} 行不安全")
        relative_name = relative_path.as_posix()
        if relative_name in expected:
            raise InstallError(f"SHA256SUMS 包含重复路径: {relative_name}")
        expected[relative_name] = checksum

    actual_files: set[str] = set()
    for path in source.rglob("*"):
        if path.name == "SHA256SUMS" or not path.is_file():
            continue
        if not _within(path, source):
            raise InstallError(f"发布包包含越界符号链接: {path}")
        relative = path.relative_to(source).as_posix()
        actual_files.add(relative)
        checksum = expected.get(relative)
        if checksum is None:
            raise InstallError(f"发布包包含未登记文件: {relative}")
        if _sha256(path) != checksum:
            raise InstallError(f"文件 checksum 不匹配: {relative}")
    missing = set(expected) - actual_files
    if missing:
        raise InstallError("发布包缺少文件: " + ", ".join(sorted(missing)))
    required_files = [
        ("neu-box-install", True),
        ("config/worker.env.example", False),
        ("systemd/neu-box-worker.service", False),
        ("share/neu-box/info/gpu_info.sh", True),
        ("share/neu-box/sandbox/v2/sandbox.sh", True),
        ("share/neu-box/sandbox/v2/device_block.o", False),
    ]
    for relative, executable_required in required_files:
        path = source / relative
        if not path.is_file():
            raise InstallError(f"发布包缺少文件: {relative}")
        if executable_required and not os.access(path, os.X_OK):
            raise InstallError(f"发布包文件不可执行: {relative}")
    for role in ROLES:
        executable = source / role / f"neu-box-{role}"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise InstallError(f"缺少可执行程序: {executable}")
    return manifest


def _release_source(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    executable_parent = Path(sys.executable).resolve().parent
    if (executable_parent / "manifest.json").is_file():
        return executable_parent
    current = Path.cwd().resolve()
    if (current / "manifest.json").is_file():
        return current
    raise InstallError("请使用 --source 指定已解压的 Neu Box 发布目录")


def _read_state(layout: Layout) -> dict:
    if not layout.state_file.is_file():
        return {
            "format": STATE_FORMAT,
            "installed_roles": [],
            "current_version": None,
            "previous": None,
        }
    try:
        state = json.loads(layout.state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"安装状态文件损坏: {exc}") from exc
    if state.get("format") != STATE_FORMAT:
        raise InstallError("不支持的安装状态格式")
    roles = state.get("installed_roles")
    accepted_roles = ROLES + LEGACY_SPLIT_ROLES
    if (
        not isinstance(roles, list)
        or any(
            not isinstance(role, str) or role not in accepted_roles
            for role in roles
        )
        or len(roles) != len(set(roles))
    ):
        raise InstallError("安装状态中的 installed_roles 无效")
    # 0.3.0 起 Master 已拆分到 neu_box_webui 仓库。旧的单仓安装状态
    # 可能同时记录 master 和 worker；worker-only 安装器只接管 worker，
    # 但保留这一瞬态标记，以便命令向操作者说明旧 Master 不会被升级。
    state.pop(_LEGACY_ROLES_STATE_KEY, None)
    legacy_roles = [role for role in roles if role in LEGACY_SPLIT_ROLES]
    state["installed_roles"] = [role for role in roles if role in ROLES]
    if legacy_roles:
        state[_LEGACY_ROLES_STATE_KEY] = legacy_roles
    current_version = state.get("current_version")
    if current_version is not None and not _valid_version(current_version):
        raise InstallError("安装状态中的 current_version 无效")
    previous = state.get("previous")
    if previous is not None:
        if not isinstance(previous, dict) or not _valid_version(previous.get("version")):
            raise InstallError("安装状态中的 previous 记录无效")
    return state


def _pop_legacy_split_roles(state: dict) -> list[str]:
    roles = state.pop(_LEGACY_ROLES_STATE_KEY, [])
    return list(roles) if isinstance(roles, list) else []


def _log_legacy_split_roles(roles: Iterable[str]) -> None:
    retired = sorted(set(roles))
    if not retired:
        return
    _log(
        "检测到旧版安装角色 " + ",".join(retired)
        + "；当前发布包只升级 worker。旧 Master 的配置、数据库和 systemd "
        "unit 会保留，但不再由本安装器管理；切换版本后请使用 "
        "neu_box_webui 仓库启动 WebUI。"
    )


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    existing_owner: tuple[int, int] | None = None
    if path.exists() and not path.is_symlink():
        metadata = path.stat()
        existing_owner = (metadata.st_uid, metadata.st_gid)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    if existing_owner:
        os.chown(temporary, *existing_owner)
    os.replace(temporary, path)


def _atomic_copy(source: Path, destination: Path, mode: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    shutil.copy2(source, temporary)
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, destination)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _new_backup_set(layout: Layout, label: str) -> Path:
    destination = layout.backups / f"{_timestamp()}-{label}"
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    return destination


def _snapshot_path(path: Path, backup_dir: Path) -> PathSnapshot:
    if path.is_symlink():
        return PathSnapshot(path, "symlink", link_target=os.readlink(path))
    if not path.exists():
        return PathSnapshot(path, "missing")
    if not path.is_file():
        raise InstallError(f"不能覆盖非普通文件: {path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / path.name
    if backup.exists() or backup.is_symlink():
        raise InstallError(f"备份路径冲突: {backup}")
    shutil.copy2(path, backup)
    return PathSnapshot(path, "file", backup=backup)


def _restore_path(snapshot: PathSnapshot) -> None:
    path = snapshot.path
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            raise InstallError(f"无法恢复文件，目标变成了目录: {path}")
        path.unlink()
    if snapshot.kind == "missing":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.kind == "symlink":
        path.symlink_to(snapshot.link_target)
        return
    if snapshot.kind == "file" and snapshot.backup:
        _atomic_copy(snapshot.backup, path)
        return
    raise InstallError(f"未知文件快照类型: {snapshot.kind}")


def _write_state(layout: Layout, state: dict) -> None:
    _atomic_write(
        layout.state_file,
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        0o600,
    )


def _switch_current(layout: Layout, release: Path) -> None:
    layout.opt.mkdir(parents=True, exist_ok=True)
    temporary = layout.opt / f".current-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(release)
    os.replace(temporary, layout.current)


def _current_release(layout: Layout) -> Path | None:
    if not layout.current.is_symlink():
        return None
    target = layout.current.resolve()
    if not _within(target, layout.releases) or not target.is_dir():
        raise InstallError(f"current 指向无效发布目录: {target}")
    return target


def _stage_release(source: Path, layout: Layout, version: str) -> Path:
    layout.releases.mkdir(parents=True, exist_ok=True)
    destination = layout.releases / version
    if destination.exists():
        installed = verify_release(destination)
        if installed.get("version") != version:
            raise InstallError(f"现有发布目录版本不一致: {destination}")
        if (destination / "SHA256SUMS").read_bytes() != (
            source / "SHA256SUMS"
        ).read_bytes():
            raise InstallError(
                f"版本 {version} 已安装，但内容与当前发布包不同；"
                "请使用新版本号构建，不允许原地覆盖发布版本"
            )
        return destination
    incoming = layout.releases / f".incoming-{version}-{os.getpid()}"
    if incoming.exists():
        shutil.rmtree(incoming)
    shutil.copytree(source, incoming, symlinks=True)
    verify_release(incoming)
    os.replace(incoming, destination)
    return destination


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        result[key] = value
    return result


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                result.append(f"{key}={remaining.pop(key)}")
                continue
        result.append(line)
    if remaining:
        result.append("")
        result.append("# Imported from the previous source-based deployment.")
        result.extend(f"{key}={value}" for key, value in sorted(remaining.items()))
    _atomic_write(path, "\n".join(result) + "\n", 0o640)


def _mapped_absolute(layout: Layout, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InstallError(f"部署配置路径必须是绝对路径: {value!r}")
    return layout.path(str(path))


def _role_config(layout: Layout, role: str) -> Path:
    return layout.config / f"{role}.env"


def _role_database(layout: Layout, role: str) -> Path:
    values = _parse_env_file(_role_config(layout, role))
    default = (
        "/var/lib/neu-box/master/master.db"
        if role == "master"
        else "/var/lib/neu-box/worker/neu_box.db"
    )
    return _mapped_absolute(layout, values.get("NEU_BOX_DB_PATH", default))


def _role_port(layout: Layout, role: str) -> int:
    values = _parse_env_file(_role_config(layout, role))
    default = "25565" if role == "master" else "59075"
    try:
        port = int(values.get("NEU_BOX_PORT", default))
    except ValueError as exc:
        raise InstallError(f"{role}.env 中 NEU_BOX_PORT 不是整数") from exc
    if not 1 <= port <= 65535:
        raise InstallError(f"{role}.env 中 NEU_BOX_PORT 超出 1-65535: {port}")
    return port


def _role_health_host(layout: Layout, role: str) -> str:
    values = _parse_env_file(_role_config(layout, role))
    listen = values.get("NEU_BOX_LISTEN", "0.0.0.0").strip()
    if listen in {"", "0.0.0.0", "*"}:
        return "127.0.0.1"
    if listen in {"::", "[::]"}:
        return "[::1]"
    if ":" in listen and not listen.startswith("["):
        return f"[{listen}]"
    return listen


def _ensure_account(layout: Layout, roles: Iterable[str]) -> tuple[int, int] | None:
    if "master" not in roles:
        return None
    if layout.root != Path("/"):
        return os.getuid(), os.getgid()
    try:
        user = pwd.getpwnam("neu-box")
    except KeyError:
        shell = "/usr/sbin/nologin" if Path("/usr/sbin/nologin").exists() else "/sbin/nologin"
        _run([
            "useradd", "--system", "--home-dir", "/var/lib/neu-box/master",
            "--shell", shell, "--user-group", "neu-box",
        ])
        user = pwd.getpwnam("neu-box")
    return user.pw_uid, user.pw_gid


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if not path.exists():
        return
    os.chown(path, uid, gid)
    for child in path.rglob("*"):
        if not child.is_symlink():
            os.chown(child, uid, gid)


def _provision_paths(layout: Layout, release: Path, roles: set[str]) -> None:
    account = _ensure_account(layout, roles)
    layout.config.mkdir(parents=True, exist_ok=True)
    layout.data.mkdir(parents=True, exist_ok=True)
    layout.logs.mkdir(parents=True, exist_ok=True)
    layout.backups.mkdir(parents=True, exist_ok=True)
    os.chmod(layout.config, 0o750)
    os.chmod(layout.data, 0o750)
    os.chmod(layout.logs, 0o750)
    os.chmod(layout.backups, 0o700)

    for role in roles:
        config = _role_config(layout, role)
        if not config.exists():
            template = release / "config" / f"{role}.env.example"
            content = template.read_text(encoding="utf-8")
            if role == "master":
                content = content.replace(
                    "SECRET_KEY=\n",
                    f"SECRET_KEY={secrets.token_hex(32)}\n",
                    1,
                )
            _atomic_write(config, content, 0o640)
            _log(f"已创建配置: {config}")
        role_data = layout.data / role
        role_data.mkdir(parents=True, exist_ok=True)
        os.chmod(role_data, 0o750)
        if role == "worker":
            (role_data / "task-logs").mkdir(parents=True, exist_ok=True)
        else:
            (role_data / "uploads").mkdir(parents=True, exist_ok=True)
            (role_data / "experiment-logs").mkdir(parents=True, exist_ok=True)
            nodes = layout.config / "nodes.json"
            if not nodes.exists():
                shutil.copy2(release / "config" / "nodes.json.example", nodes)
                os.chmod(nodes, 0o640)

    if account:
        uid, gid = account
        if "master" in roles:
            config_uid = 0 if layout.root == Path("/") else uid
            os.chown(layout.config, config_uid, gid)
            os.chown(layout.data, config_uid, gid)
            _chown_tree(layout.data / "master", uid, gid)
            _chown_tree(layout.logs, uid, gid)
            os.chown(_role_config(layout, "master"), config_uid, gid)
            nodes = layout.config / "nodes.json"
            if nodes.exists():
                os.chown(nodes, uid, gid)


def _import_legacy(layout: Layout, role: str, args: argparse.Namespace) -> None:
    legacy_config = getattr(args, "legacy_config", None)
    legacy_database = getattr(args, "legacy_database", None)
    legacy_nodes = getattr(args, "legacy_nodes", None)

    if legacy_config:
        source = Path(legacy_config).expanduser().resolve()
        if not source.is_file():
            raise InstallError(f"旧配置不存在: {source}")
        old = _parse_env_file(source)
        common_mapping = {
            "listen": "NEU_BOX_LISTEN",
            "port": "NEU_BOX_PORT",
            "LOG_LEVEL": "LOG_LEVEL",
        }
        role_mapping = {
            "master": {
                "poll_interval": "NEU_BOX_POLL_INTERVAL",
                "SECRET_KEY": "SECRET_KEY",
                "ADMIN_USER": "ADMIN_USER",
                "ADMIN_PASS": "ADMIN_PASS",
                "upload_max_size": "NEU_BOX_UPLOAD_MAX_SIZE",
            },
            "worker": {
                "device_filter": "NEU_BOX_DEVICE_FILTER",
                "sandbox_reaper_interval": "NEU_BOX_SANDBOX_REAPER_INTERVAL",
                "command_timeout": "NEU_BOX_COMMAND_TIMEOUT",
                "command_max_completed": "NEU_BOX_COMMAND_MAX_COMPLETED",
                "command_queue_recent": "NEU_BOX_COMMAND_QUEUE_RECENT",
            },
        }
        updates: dict[str, str] = {}
        for old_key, new_key in {**common_mapping, **role_mapping[role]}.items():
            value = old.get(old_key, "")
            if value:
                updates[new_key] = value
        if role == "worker" and old.get("dev_info_script_path"):
            updates["NEU_BOX_DEVICE_INFO_SCRIPT"] = (
                "/opt/neu-box/current/share/neu-box/info/gpu_info.sh"
            )
        _update_env_file(_role_config(layout, role), updates)
        _log(f"已导入旧配置（路径类配置使用新稳定目录）: {source}")

    if legacy_nodes:
        if role != "master":
            raise InstallError("--legacy-nodes 只能用于 Master")
        source = Path(legacy_nodes).expanduser().resolve()
        if not source.is_file():
            raise InstallError(f"旧节点配置不存在: {source}")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallError(f"旧节点配置不是有效 JSON: {exc}") from exc
        if not isinstance(payload.get("nodes_pool"), list):
            raise InstallError("旧节点配置缺少 nodes_pool 数组")
        destination = layout.config / "nodes.json"
        temporary = destination.with_name(f".{destination.name}.import-{os.getpid()}")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        os.chmod(destination, 0o640)
        if layout.root == Path("/"):
            user = pwd.getpwnam("neu-box")
            os.chown(destination, user.pw_uid, user.pw_gid)
        _log(f"已导入旧节点配置: {source}")

    if legacy_database:
        source = Path(legacy_database).expanduser().resolve()
        if not source.is_file():
            raise InstallError(f"旧数据库不存在: {source}")
        destination = _role_database(layout, role)
        if source == destination:
            raise InstallError("旧数据库不能与新稳定数据库使用同一路径")
        if destination.exists():
            raise InstallError(
                f"目标数据库已存在，拒绝静默跳过旧库导入: {destination}"
            )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _sqlite_backup(source, destination)
            _log(f"已导入旧数据库: {source} -> {destination}")
            if role == "master" and layout.root == Path("/"):
                user = pwd.getpwnam("neu-box")
                _chown_tree(destination.parent, user.pw_uid, user.pw_gid)


def _install_systemd(layout: Layout, release: Path, roles: set[str]) -> None:
    layout.units.mkdir(parents=True, exist_ok=True)
    for role in roles:
        source = release / "systemd" / f"neu-box-{role}.service"
        destination = layout.units / source.name
        _atomic_copy(source, destination, 0o644)


def _install_self(layout: Layout, release: Path) -> None:
    source = release / "neu-box-install"
    if not source.is_file() or not os.access(source, os.X_OK):
        raise InstallError(f"发布包缺少安装器: {source}")
    _atomic_copy(source, layout.sbin / "neu-box-install", 0o755)
    launcher = release / "run.sh"
    if launcher.is_file() and os.access(launcher, os.X_OK):
        _atomic_copy(launcher, layout.sbin / "neu-box", 0o755)


def _systemd_available(layout: Layout, no_systemd: bool) -> bool:
    return layout.root == Path("/") and not no_systemd


def _selinux_enforcing() -> bool:
    try:
        return Path("/sys/fs/selinux/enforce").read_text(
            encoding="ascii"
        ).strip() == "1"
    except OSError:
        return False


def _restore_selinux_contexts(
    layout: Layout,
    paths: Iterable[Path],
    *,
    recursive: bool = False,
) -> None:
    """Apply host policy labels after copying files from a release archive.

    ``shutil.copy2``/``copytree`` can preserve a build tree's SELinux xattrs.
    A label such as ``unlabeled_t`` makes systemd fail with status 203/EXEC
    even though an unconfined interactive shell can execute the same file.
    ``restorecon`` is a no-op for paths without a policy entry and is skipped
    on staged roots and hosts where policycoreutils is not installed.
    """
    if layout.root != Path("/"):
        return
    restorecon = shutil.which("restorecon")
    if not restorecon:
        if _selinux_enforcing():
            raise InstallError(
                "SELinux 处于 Enforcing，但缺少 restorecon（policycoreutils）"
            )
        return
    existing = [str(path) for path in paths if path.exists() or path.is_symlink()]
    if existing:
        flags = "-RF" if recursive else "-F"
        _run([restorecon, flags, *existing])


def _service_name(role: str) -> str:
    return f"neu-box-{role}.service"


def _capture_service_states(
    roles: Iterable[str],
    enabled: bool,
) -> dict[str, ServiceState]:
    if not enabled:
        return {}
    result: dict[str, ServiceState] = {}
    for role in roles:
        service = _service_name(role)
        active = _run(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
        ).returncode == 0
        unit_enabled = _run(
            ["systemctl", "is-enabled", "--quiet", service],
            check=False,
        ).returncode == 0
        result[role] = ServiceState(
            active=active,
            enabled=unit_enabled,
        )
    return result


def _stop_services(
    roles: Iterable[str],
    enabled: bool,
) -> None:
    if not enabled:
        return
    for role in roles:
        service = _service_name(role)
        active = _run(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
        ).returncode == 0
        if active:
            _run(["systemctl", "stop", service])

def _enable_and_start(roles: Iterable[str], enabled: bool, start: bool) -> None:
    if not enabled:
        return
    _run(["systemctl", "daemon-reload"])
    for role in roles:
        service = _service_name(role)
        _run(["systemctl", "enable", service])
        if start:
            _run(["systemctl", "start", service])


def _restore_service_states(
    states: dict[str, ServiceState],
    enabled: bool,
) -> None:
    if not enabled:
        return
    _run(["systemctl", "daemon-reload"])
    for role, state in states.items():
        service = _service_name(role)
        action = "enable" if state.enabled else "disable"
        _run(["systemctl", action, service], check=False)
        if state.active:
            _run(["systemctl", "start", service])
        else:
            _run(["systemctl", "stop", service], check=False)


def _role_command(
    layout: Layout,
    release: Path,
    role: str,
    arguments: list[str],
    *,
    database_override: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = release / role / f"neu-box-{role}"
    config = _role_config(layout, role)
    environment = os.environ.copy()
    if database_override is not None:
        environment["NEU_BOX_DB_PATH"] = str(database_override)
    elif layout.root != Path("/"):
        environment["NEU_BOX_DB_PATH"] = str(_role_database(layout, role))
    return _run(
        [str(executable), "--config", str(config), *arguments],
        capture=True,
        env=environment,
    )


def _sqlite_backup(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    source_conn = sqlite3.connect(database)
    target_conn = sqlite3.connect(temporary)
    try:
        source_conn.backup(target_conn)
        target_conn.commit()
        result = str(target_conn.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            raise InstallError(f"备份完整性检查失败: {result}")
    finally:
        target_conn.close()
        source_conn.close()
    os.replace(temporary, destination)


def _backup_databases(
    layout: Layout,
    roles: Iterable[str],
    backup_set: Path,
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for role in roles:
        database = _role_database(layout, role)
        if not database.is_file():
            result[role] = None
            continue
        destination = backup_set / f"{role}.db"
        _sqlite_backup(database, destination)
        result[role] = str(destination)
        _log(f"已备份 {role} 数据库: {destination}")
    return result


def _backup_configs(
    layout: Layout,
    roles: Iterable[str],
    backup_set: Path,
) -> str | None:
    existing: list[Path] = []
    for role in roles:
        config = _role_config(layout, role)
        if config.is_file():
            existing.append(config)
    nodes = layout.config / "nodes.json"
    if "master" in roles and nodes.is_file():
        existing.append(nodes)
    if not existing:
        return None
    destination = backup_set / "config"
    destination.mkdir(parents=True, exist_ok=True)
    for source in existing:
        shutil.copy2(source, destination / source.name)
    _log(f"已备份配置: {destination}")
    return str(destination)


def _preflight_migrations(
    layout: Layout,
    release: Path,
    roles: Iterable[str],
    backups: dict[str, str | None],
) -> None:
    for role in roles:
        backup = backups.get(role)
        if not backup:
            continue
        with tempfile.TemporaryDirectory(prefix=f"neu-box-{role}-migration-") as raw:
            test_database = Path(raw) / f"{role}.db"
            shutil.copy2(backup, test_database)
            _role_command(
                layout,
                release,
                role,
                ["db", "migrate"],
                database_override=test_database,
            )
            _role_command(
                layout,
                release,
                role,
                ["db", "check"],
                database_override=test_database,
            )
        _log(f"{role} 数据库副本迁移测试通过")


def _migrate_live(layout: Layout, release: Path, roles: Iterable[str]) -> None:
    for role in roles:
        result = _role_command(layout, release, role, ["db", "migrate"])
        if result.stdout:
            print(result.stdout.rstrip())
        database = _role_database(layout, role)
        if role == "master" and layout.root == Path("/"):
            user = pwd.getpwnam("neu-box")
            _chown_tree(database.parent, user.pw_uid, user.pw_gid)


def _restore_databases(
    layout: Layout,
    roles: Iterable[str],
    backups: dict[str, str | None],
) -> None:
    for role in roles:
        database = _role_database(layout, role)
        backup = backups.get(role)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        if backup:
            database.parent.mkdir(parents=True, exist_ok=True)
            temporary = database.with_name(f".{database.name}.restore-{os.getpid()}")
            shutil.copy2(backup, temporary)
            os.replace(temporary, database)
            _log(f"已恢复 {role} 数据库: {backup}")
        elif database.exists():
            database.unlink()
            _log(f"已移除升级时新建的 {role} 数据库")
    if "master" in roles and layout.root == Path("/"):
        user = pwd.getpwnam("neu-box")
        _chown_tree(layout.data / "master", user.pw_uid, user.pw_gid)


def _healthcheck(layout: Layout, roles: Iterable[str], version: str) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for role in roles:
        url = (
            f"http://{_role_health_host(layout, role)}:"
            f"{_role_port(layout, role)}/healthz"
        )
        last_error = ""
        for _ in range(30):
            try:
                with opener.open(url, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if (
                    response.status == 200
                    and payload.get("status") == "ok"
                    and payload.get("role") == role
                    and payload.get("version") == version
                ):
                    _log(f"{role} 健康检查通过: {url}")
                    break
                last_error = f"unexpected response: {payload!r}"
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            time.sleep(1)
        else:
            raise InstallError(f"{role} 健康检查失败: {last_error}")


def _preflight_host(layout: Layout, release: Path, roles: set[str], systemd: bool) -> None:
    if layout.root == Path("/") and os.geteuid() != 0:
        raise InstallError("安装、升级和回滚需要 root 权限")
    if systemd:
        commands = ["systemctl"]
        if "master" in roles:
            commands.append("useradd")
        for command in commands:
            if not shutil.which(command):
                raise InstallError(f"缺少系统命令: {command}")
    if "worker" in roles and layout.root == Path("/"):
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise InstallError("Worker 要求 cgroup v2")
        for command in ("bash", "bpftool", "busctl"):
            if not shutil.which(command):
                raise InstallError(f"Worker 缺少系统命令: {command}")
        bpf_object = release / "share" / "neu-box" / "sandbox" / "v2" / "device_block.o"
        if not bpf_object.is_file():
            raise InstallError("发布包缺少预编译 device_block.o")


def deploy(args: argparse.Namespace, layout: Layout) -> int:
    source = _release_source(args.source)
    manifest = verify_release(source)
    version = str(manifest["version"])
    requested = {args.role}
    state = _read_state(layout)
    legacy_split_roles = _pop_legacy_split_roles(state)
    _log_legacy_split_roles(legacy_split_roles)
    installed_roles = set(state.get("installed_roles") or [])
    roles = installed_roles | requested
    systemd = _systemd_available(layout, args.no_systemd)
    _preflight_host(layout, source, roles, systemd)

    current = _current_release(layout)
    previous_version = state.get("current_version")
    has_legacy_import = bool(
        getattr(args, "legacy_config", None)
        or getattr(args, "legacy_database", None)
        or getattr(args, "legacy_nodes", None)
    )
    if has_legacy_import and (
        args.command != "install"
        or args.role in installed_roles
    ):
        raise InstallError(
            "legacy 导入只允许该角色首次 install，"
            "且 --role 必须明确为 worker"
        )
    if (current is None) != (previous_version is None):
        raise InstallError("current 符号链接与安装状态记录不一致")
    if current and current.name != previous_version:
        raise InstallError("current 符号链接与安装状态记录不一致")
    if args.command == "upgrade":
        if current is None:
            raise InstallError("尚未安装 Neu Box；首次部署请使用 install")
        if not requested.issubset(installed_roles):
            raise InstallError("upgrade 不能增加角色；请先用 install 增加角色")
    elif current is not None and version != previous_version:
        raise InstallError("install 不能切换版本；已有部署请使用 upgrade")
    release = _stage_release(source, layout, version)
    _restore_selinux_contexts(layout, [release], recursive=True)
    _provision_paths(layout, release, roles)
    if has_legacy_import:
        _import_legacy(layout, args.role, args)
    _restore_selinux_contexts(layout, [layout.config], recursive=True)
    _restore_selinux_contexts(layout, [
        layout.opt,
        layout.current,
        layout.data,
        *(layout.data / role for role in roles),
        *(_role_database(layout, role) for role in roles),
        layout.logs,
        layout.backups,
    ])
    affected = sorted(roles)
    backup_set = _new_backup_set(layout, f"before-{version}")
    snapshots: list[PathSnapshot] = []
    service_states: dict[str, ServiceState] = {}
    backups: dict[str, str | None] = {}
    config_backup: str | None = None
    backup_complete = False
    services_touched = False
    switched = False
    try:
        snapshot_dir = backup_set / "replaced-files"
        if systemd:
            for role in roles:
                snapshots.append(_snapshot_path(
                    layout.units / f"neu-box-{role}.service",
                    snapshot_dir,
                ))
        if layout.root == Path("/"):
            snapshots.append(_snapshot_path(
                layout.sbin / "neu-box-install",
                snapshot_dir,
            ))
            snapshots.append(_snapshot_path(
                layout.sbin / "neu-box",
                snapshot_dir,
            ))
        service_states = _capture_service_states(affected, systemd)
        services_touched = True
        _stop_services(affected, systemd)
        backups = _backup_databases(layout, affected, backup_set)
        config_backup = _backup_configs(layout, affected, backup_set)
        backup_complete = True
        _preflight_migrations(layout, release, affected, backups)
        _migrate_live(layout, release, affected)
        _switch_current(layout, release)
        switched = True
        if systemd:
            _install_systemd(layout, release, roles)
        _restore_selinux_contexts(layout, [
            layout.current,
            *(layout.units / f"neu-box-{role}.service" for role in roles),
        ])
        _enable_and_start(affected, systemd, not args.no_start)
        if systemd and not args.no_start:
            _healthcheck(layout, affected, version)
        if layout.root == Path("/"):
            _install_self(layout, release)
        _restore_selinux_contexts(layout, [
            layout.sbin / "neu-box-install",
            layout.sbin / "neu-box",
        ])

        previous = state.get("previous")
        if current and previous_version and previous_version != version:
            previous = {
                "version": previous_version,
                "database_backups": backups,
                "config_backup": config_backup,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        _write_state(layout, {
            "format": STATE_FORMAT,
            "installed_roles": sorted(roles),
            "current_version": version,
            "previous": previous,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        _log("部署失败，正在恢复升级前状态")
        if services_touched:
            _stop_services(affected, systemd)
        if backup_complete:
            _restore_databases(layout, affected, backups)
        if current:
            _switch_current(layout, current)
        elif switched and layout.current.is_symlink():
            layout.current.unlink()
        for snapshot in reversed(snapshots):
            _restore_path(snapshot)
        _restore_service_states(service_states, systemd)
        raise
    _log(f"部署完成: version={version}, roles={','.join(sorted(roles))}")
    return 0


def rollback(args: argparse.Namespace, layout: Layout) -> int:
    state = _read_state(layout)
    legacy_split_roles = _pop_legacy_split_roles(state)
    _log_legacy_split_roles(legacy_split_roles)
    roles = set(state.get("installed_roles") or [])
    previous = state.get("previous")
    if not roles or not previous:
        raise InstallError("没有可回滚的上一版本")
    current = _current_release(layout)
    if current is None:
        raise InstallError("当前版本符号链接不存在")
    if current.name != state.get("current_version"):
        raise InstallError("current 符号链接与安装状态记录不一致")
    previous_version = str(previous.get("version", ""))
    if not _valid_version(previous_version):
        raise InstallError(f"回滚状态中的版本号无效: {previous_version!r}")
    previous_release = (layout.releases / previous_version).resolve()
    if not _within(previous_release, layout.releases):
        raise InstallError("回滚版本目录越过 releases 边界")
    manifest = verify_release(previous_release)
    if str(manifest.get("version")) != previous_version:
        raise InstallError("回滚目录名与 manifest 版本不一致")
    systemd = _systemd_available(layout, args.no_systemd)
    _preflight_host(layout, previous_release, roles, systemd)
    if not args.yes:
        if not sys.stdin.isatty():
            raise InstallError("回滚会恢复旧数据库；非交互运行必须提供 --yes")
        answer = input(
            f"将从 {state.get('current_version')} 回滚到 {previous_version}，"
            "并恢复升级前数据库，继续？[y/N] "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            _log("已取消")
            return 0

    affected = sorted(roles)
    backup_set = _new_backup_set(layout, "before-rollback")
    snapshots: list[PathSnapshot] = []
    service_states: dict[str, ServiceState] = {}
    rescue: dict[str, str | None] = {}
    rescue_config: str | None = None
    backup_complete = False
    services_touched = False
    try:
        snapshot_dir = backup_set / "replaced-files"
        if systemd:
            for role in roles:
                snapshots.append(_snapshot_path(
                    layout.units / f"neu-box-{role}.service",
                    snapshot_dir,
                ))
        if layout.root == Path("/"):
            snapshots.append(_snapshot_path(
                layout.sbin / "neu-box-install",
                snapshot_dir,
            ))
            snapshots.append(_snapshot_path(
                layout.sbin / "neu-box",
                snapshot_dir,
            ))
        service_states = _capture_service_states(affected, systemd)
        services_touched = True
        _stop_services(affected, systemd)
        rescue = _backup_databases(layout, affected, backup_set)
        rescue_config = _backup_configs(layout, affected, backup_set)
        backup_complete = True
        _restore_databases(
            layout,
            affected,
            dict(previous.get("database_backups") or {}),
        )
        _switch_current(layout, previous_release)
        if systemd:
            _install_systemd(layout, previous_release, roles)
        _enable_and_start(affected, systemd, not args.no_start)
        if systemd and not args.no_start:
            _healthcheck(layout, affected, str(manifest["version"]))
        if layout.root == Path("/"):
            _install_self(layout, previous_release)

        _write_state(layout, {
            "format": STATE_FORMAT,
            "installed_roles": affected,
            "current_version": previous_version,
            "previous": {
                "version": str(state.get("current_version")),
                "database_backups": rescue,
                "config_backup": rescue_config,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        _log("回滚失败，正在恢复回滚前状态")
        if services_touched:
            _stop_services(affected, systemd)
        if backup_complete:
            _restore_databases(layout, affected, rescue)
        _switch_current(layout, current)
        for snapshot in reversed(snapshots):
            _restore_path(snapshot)
        _restore_service_states(service_states, systemd)
        raise
    _log(f"已回滚到 {previous_version}")
    return 0


def status(layout: Layout) -> int:
    state = _read_state(layout)
    legacy_split_roles = _pop_legacy_split_roles(state)
    current = _current_release(layout)
    result = dict(state)
    if legacy_split_roles:
        result["legacy_split_roles"] = sorted(legacy_split_roles)
    result["current_path"] = str(current) if current else None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neu-box-install",
        description=(
            "安装、升级或回滚 Neu Box 的版本化发布包。\n"
            "install/upgrade 自动校验包、备份 SQLite、试跑并执行迁移、\n"
            "切换版本、启动服务和检查健康状态；失败时恢复原状态。"
        ),
        epilog=(
            "示例：\n"
            "  neu-box-install install --role worker\n"
            "  neu-box-install upgrade --role worker --source /tmp/neu-box-0.4.0\n"
            "  neu-box-install rollback\n"
            "  neu-box-install status\n\n"
            "--root 和 --no-systemd 是发布测试选项，必须写在子命令之前。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        default="/",
        metavar="PATH",
        help="把绝对安装路径映射到 PATH；默认 /（测试用）",
    )
    parser.add_argument(
        "--no-systemd",
        action="store_true",
        help="不调用 systemd 或做 HTTP 健康检查（测试用）",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_deploy_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--role",
            choices=ROLES,
            required=True,
            help="本次安装的角色：worker",
        )
        command.add_argument(
            "--source",
            metavar="DIR",
            help="已解压发布目录；默认取安装器所在目录",
        )
        command.add_argument(
            "--no-start",
            action="store_true",
            help="安装并启用 unit，但暂不启动服务或做健康检查",
        )

    install_parser = commands.add_parser(
        "install",
        help="首次安装或为本机增加角色",
        description="首次安装 Neu Box；已有配置和数据不会被模板覆盖。",
    )
    add_deploy_arguments(install_parser)
    install_parser.add_argument(
        "--legacy-config",
        metavar="FILE",
        help="首次安装单个角色时导入旧 .env",
    )
    install_parser.add_argument(
        "--legacy-database",
        metavar="FILE",
        help="首次安装单个角色时复制旧 SQLite 数据库",
    )
    install_parser.add_argument(
        "--legacy-nodes",
        metavar="FILE",
        help="首次安装 Master 时导入旧 config.json",
    )

    upgrade_parser = commands.add_parser(
        "upgrade",
        help="升级所有已安装角色",
        description=(
            "升级到指定发布版本。--role 用于单角色机器；如果本机已经安装\n"
            "多个角色，它们会一起升级，以保持 current 指向同一版本。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_deploy_arguments(upgrade_parser)

    rollback_parser = commands.add_parser(
        "rollback",
        help="回滚程序和升级前数据库",
    )
    rollback_parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过会恢复旧数据库的交互确认",
    )
    rollback_parser.add_argument(
        "--no-start",
        action="store_true",
        help="回滚后暂不启动服务或做健康检查",
    )
    commands.add_parser("status", help="显示已安装角色和版本")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = Path(args.root).expanduser().resolve()
        if args.command != "status" and root != Path("/"):
            root.mkdir(parents=True, exist_ok=True)
        layout = Layout(root)
        if args.command in {"install", "upgrade"}:
            return deploy(args, layout)
        if args.command == "rollback":
            return rollback(args, layout)
        return status(layout)
    except (
        InstallError,
        OSError,
        ValueError,
        sqlite3.Error,
        subprocess.CalledProcessError,
    ) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            details = (exc.stderr or exc.stdout or "").strip()
            message = f"命令失败（exit={exc.returncode}）"
            if details:
                message += f": {details}"
        else:
            message = str(exc)
        print(f"neu-box-install: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
