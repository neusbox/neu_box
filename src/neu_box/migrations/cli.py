"""Reusable command-line interface for role-specific SQLite databases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from neu_box.config import user_data_dir
from neu_box.migrations.engine import (
    SchemaStatus,
    backup_database,
    check_database,
    migrate_database,
    restore_database,
    schema_status,
)


def add_database_commands(subparsers: argparse._SubParsersAction) -> None:
    database = subparsers.add_parser(
        "db",
        help="检查、迁移或备份 SQLite 数据库",
    )
    commands = database.add_subparsers(dest="db_command", required=True)
    commands.add_parser("status", help="显示当前和待执行 schema 版本")
    commands.add_parser("migrate", help="执行所有待处理迁移")
    commands.add_parser("check", help="执行完整性检查并校验迁移历史")
    backup = commands.add_parser("backup", help="创建一致的 SQLite 备份")
    backup.add_argument(
        "--output-dir",
        help="备份目录；默认读取 NEU_BOX_BACKUP_DIR",
    )
    restore = commands.add_parser(
        "restore", help="从备份原子恢复数据库（服务必须已停止）",
    )
    restore.add_argument(
        "--input", required=True, help="要恢复的 SQLite 备份文件",
    )
    restore.add_argument(
        "--output-dir",
        help="替换前安全备份目录；默认读取 NEU_BOX_BACKUP_DIR",
    )


def _print_status(status: SchemaStatus) -> None:
    print(json.dumps({
        "database": str(status.database),
        "state": status.state,
        "current": status.current,
        "latest": status.latest,
        "pending": list(status.pending),
    }, ensure_ascii=False, indent=2))


def run_database_command(
    args: argparse.Namespace,
    *,
    role: str,
    database: str,
    migrations_package: str,
    required_columns: Mapping[str, Sequence[str]],
    required_indexes: Sequence[str],
    service: str | None = None,
) -> int:
    command = args.db_command
    if command == "status":
        _print_status(schema_status(database, migrations_package))
        return 0
    if command == "migrate":
        status = migrate_database(
            database,
            migrations_package,
            required_columns,
            required_indexes,
        )
        _print_status(status)
        return 0
    if command == "check":
        status = check_database(
            database,
            migrations_package,
            required_columns,
            required_indexes,
        )
        _print_status(status)
        return 0
    if command == "backup":
        raw_dir = args.output_dir or os.getenv("NEU_BOX_BACKUP_DIR", "").strip()
        backup_dir = (
            Path(raw_dir).expanduser().resolve()
            if raw_dir
            else user_data_dir(role).parent / "backups"
        )
        destination = backup_database(database, backup_dir, role)
        print(destination)
        return 0
    if command == "restore":
        if service:
            import subprocess
            try:
                active = subprocess.run(
                    ["systemctl", "is-active", "--quiet", service],
                    check=False,
                ).returncode == 0
            except OSError as exc:
                raise RuntimeError(f"无法检查 {service} 状态: {exc}") from exc
            if active:
                raise RuntimeError(
                    f"{service} 仍在运行，请先执行 neuboxctl pause"
                )
        raw_dir = args.output_dir or os.getenv("NEU_BOX_BACKUP_DIR", "").strip()
        backup_dir = (
            Path(raw_dir).expanduser().resolve()
            if raw_dir
            else user_data_dir(role).parent / "backups"
        )
        safety = restore_database(
            database, args.input, backup_dir, role,
        )
        if safety is not None:
            print(f"恢复前数据库备份: {safety}")
        print(database)
        return 0
    raise RuntimeError(f"未知数据库命令: {command}")
