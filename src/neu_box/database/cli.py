"""Reusable command-line interface for role-specific SQLite databases."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from neu_box.config import user_data_dir
from neu_box.database.migrations import (
    SchemaStatus,
    backup_database,
    check_database,
    migrate_database,
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
    raise RuntimeError(f"未知数据库命令: {command}")
