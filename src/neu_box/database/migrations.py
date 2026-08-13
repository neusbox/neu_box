"""Small, explicit SQLite migration runner used by Master and Worker."""

from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


_MIGRATION_RE = re.compile(
    r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.(?P<kind>sql|py)$"
)
_HISTORY_TABLE_SQL = """
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """The database schema cannot safely be migrated or used."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    kind: str
    package: str
    filename: str
    content: bytes

    @property
    def checksum(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class SchemaStatus:
    database: Path
    state: str
    current: int
    latest: int
    pending: tuple[int, ...]


def discover_migrations(package: str) -> tuple[Migration, ...]:
    """Load ordered migration resources from a Python package."""
    migrations: list[Migration] = []
    root = importlib.resources.files(package)
    for resource in root.iterdir():
        match = _MIGRATION_RE.fullmatch(resource.name)
        if not match:
            continue
        migrations.append(Migration(
            version=int(match.group("version")),
            name=match.group("name"),
            kind=match.group("kind"),
            package=package,
            filename=resource.name,
            content=resource.read_bytes(),
        ))
    migrations.sort(key=lambda item: item.version)
    versions = [item.version for item in migrations]
    if not migrations:
        raise MigrationError(f"迁移包 {package} 中没有迁移文件")
    if len(versions) != len(set(versions)):
        raise MigrationError(f"迁移包 {package} 包含重复版本")
    if versions[0] != 1:
        raise MigrationError(f"迁移包 {package} 必须从 0001 开始")
    expected = list(range(1, versions[-1] + 1))
    if versions != expected:
        raise MigrationError(
            f"迁移包 {package} 版本不连续: {versions!r}"
        )
    return tuple(migrations)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    quoted = table.replace('"', '""')
    return {
        str(row["name"])
        for row in conn.execute(f'PRAGMA table_info("{quoted}")').fetchall()
    }


def validate_existing_schema(
    conn: sqlite3.Connection,
    required_columns: Mapping[str, Sequence[str]],
    required_indexes: Sequence[str] = (),
) -> None:
    """Validate a pre-migration database before adopting it as schema v1.

    Extra legacy columns and tables are allowed because older Neu Box releases
    left harmless columns behind. Missing current objects are never guessed.
    """
    problems: list[str] = []
    for table, columns in required_columns.items():
        if not _table_exists(conn, table):
            problems.append(f"缺少表 {table}")
            continue
        missing = set(columns) - _columns(conn, table)
        if missing:
            problems.append(
                f"表 {table} 缺少字段 {', '.join(sorted(missing))}"
            )
    existing_indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    missing_indexes = set(required_indexes) - existing_indexes
    if missing_indexes:
        problems.append(
            "缺少索引 " + ", ".join(sorted(missing_indexes))
        )
    if problems:
        raise MigrationError(
            "现有数据库不是可识别的 Neu Box schema v1："
            + "；".join(problems)
        )


def _read_history(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    if not _table_exists(conn, "schema_migrations"):
        return {}
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row["version"]): row for row in rows}


def _validate_history(
    migrations: Sequence[Migration],
    history: Mapping[int, sqlite3.Row],
) -> None:
    by_version = {item.version: item for item in migrations}
    for version, row in history.items():
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"数据库包含当前程序未知的迁移版本 {version:04d}"
            )
        if row["name"] != migration.name:
            raise MigrationError(
                f"迁移 {version:04d} 名称不一致："
                f"数据库={row['name']} 文件={migration.name}"
            )
        if row["checksum"] != migration.checksum:
            raise MigrationError(
                f"迁移 {version:04d}_{migration.name} 已被修改，checksum 不一致"
            )
    applied = sorted(history)
    if applied and applied != list(range(1, applied[-1] + 1)):
        raise MigrationError(f"数据库迁移历史不连续: {applied!r}")


def _execute_sql(conn: sqlite3.Connection, content: bytes) -> None:
    script = content.decode("utf-8")
    buffer = ""
    for char in script:
        buffer += char
        if char != ";" or not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            conn.execute(statement)
    remainder = "\n".join(
        line for line in buffer.splitlines()
        if line.strip() and not line.lstrip().startswith("--")
    ).strip()
    if remainder:
        raise MigrationError("SQL 迁移末尾包含不完整语句")


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> None:
    def deny_transaction_control(
        action: int,
        _argument_one: str | None,
        _argument_two: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_transaction_control)
    try:
        if migration.kind == "sql":
            _execute_sql(conn, migration.content)
            return
        module_name = f"{migration.package}.{migration.filename[:-3]}"
        module = importlib.import_module(module_name)
        upgrade = getattr(module, "upgrade", None)
        if not callable(upgrade):
            raise MigrationError(f"Python 迁移 {module_name} 缺少 upgrade(conn)")
        upgrade(conn)
    finally:
        conn.set_authorizer(None)


def schema_status(database: os.PathLike[str] | str, package: str) -> SchemaStatus:
    path = Path(database).expanduser().resolve()
    migrations = discover_migrations(package)
    latest = migrations[-1].version
    if not path.exists():
        return SchemaStatus(path, "missing", 0, latest,
                            tuple(item.version for item in migrations))
    with _connect(path) as conn:
        tables = _user_tables(conn)
        if not tables:
            return SchemaStatus(path, "empty", 0, latest,
                                tuple(item.version for item in migrations))
        history = _read_history(conn)
        if not history:
            if tables == {"schema_migrations"}:
                return SchemaStatus(
                    path,
                    "empty",
                    0,
                    latest,
                    tuple(item.version for item in migrations),
                )
            return SchemaStatus(path, "untracked", 0, latest,
                                tuple(item.version for item in migrations))
        _validate_history(migrations, history)
        current = max(history, default=0)
        pending = tuple(
            item.version for item in migrations if item.version not in history
        )
        return SchemaStatus(
            path,
            "current" if not pending else "pending",
            current,
            latest,
            pending,
        )


def migrate_database(
    database: os.PathLike[str] | str,
    package: str,
    required_columns: Mapping[str, Sequence[str]],
    required_indexes: Sequence[str] = (),
) -> SchemaStatus:
    """Adopt a known existing v1 database and apply every pending migration."""
    path = Path(database).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    migrations = discover_migrations(package)
    with _connect(path) as conn:
        tables = _user_tables(conn)
        history = _read_history(conn)

        if tables and "schema_migrations" not in tables:
            validate_existing_schema(
                conn, required_columns, required_indexes,
            )
            first = migrations[0]
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(_HISTORY_TABLE_SQL)
                conn.execute(
                    "INSERT INTO schema_migrations "
                    "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (
                        first.version,
                        first.name,
                        first.checksum,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            history = _read_history(conn)
        elif history == {} and tables - {"schema_migrations"}:
            raise MigrationError(
                "数据库包含业务表，但 schema_migrations 没有任何记录；"
                "拒绝猜测或自动接管该结构"
            )
        elif "schema_migrations" not in tables:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(_HISTORY_TABLE_SQL)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            history = {}

        _validate_history(migrations, history)
        for migration in migrations:
            if migration.version in history:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                _apply_one(conn, migration)
                if not conn.in_transaction:
                    raise MigrationError(
                        f"迁移 {migration.filename} 非法提交了自己的事务"
                    )
                conn.execute(
                    "INSERT INTO schema_migrations "
                    "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            history = _read_history(conn)

        _validate_history(migrations, history)
        validate_existing_schema(conn, required_columns, required_indexes)
    return schema_status(path, package)


def require_current_schema(
    database: os.PathLike[str] | str,
    package: str,
    required_columns: Mapping[str, Sequence[str]],
    required_indexes: Sequence[str] = (),
) -> None:
    status = schema_status(database, package)
    if status.state != "current":
        raise MigrationError(
            f"数据库 schema 未就绪：state={status.state}, "
            f"current={status.current}, latest={status.latest}；"
            "请先执行对应服务的 `db migrate`"
        )
    with _connect(status.database) as conn:
        validate_existing_schema(conn, required_columns, required_indexes)


def check_database(
    database: os.PathLike[str] | str,
    package: str,
    required_columns: Mapping[str, Sequence[str]],
    required_indexes: Sequence[str] = (),
) -> SchemaStatus:
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"数据库不存在: {path}")
    with _connect(path) as conn:
        result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if result != "ok":
        raise MigrationError(f"SQLite integrity_check 失败: {result}")
    status = schema_status(path, package)
    if status.state not in {"current", "pending", "untracked"}:
        raise MigrationError(f"数据库状态不可用: {status.state}")
    with _connect(path) as conn:
        validate_existing_schema(conn, required_columns, required_indexes)
    return status


def backup_database(
    database: os.PathLike[str] | str,
    backup_dir: os.PathLike[str] | str,
    role: str,
) -> Path:
    """Create and verify a consistent SQLite backup using its backup API."""
    source_path = Path(database).expanduser().resolve()
    if not source_path.is_file():
        raise MigrationError(f"数据库不存在: {source_path}")
    destination_dir = Path(backup_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = destination_dir / f"{role}-{timestamp}.db"
    temporary = destination.with_suffix(".db.tmp")
    if temporary.exists():
        temporary.unlink()
    source = _connect(source_path)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        target.commit()
        result = str(target.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            raise MigrationError(f"备份完整性检查失败: {result}")
    finally:
        target.close()
        source.close()
    os.replace(temporary, destination)
    return destination
