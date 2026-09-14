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


def _legacy_worker_version(conn: sqlite3.Connection) -> int | None:
    """Identify the known pre-history Worker schema without guessing.

    The first RPM release used the same tables as migration 0001 but did not
    have ``schema_migrations``.  We can safely adopt that database only when
    its table and index names match a known migration boundary exactly;
    unknown extensions remain an explicit operator migration task.
    """
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) if row[0] != 'sqlite_sequence'
    }
    expected_tables = {'tasks', 'sandboxes'}
    if tables != expected_tables:
        return None

    # The baseline is safe only when the original table indexes are still
    # present.  Do this check before creating ``schema_migrations`` so a
    # malformed legacy DB is rejected without leaving a partially adopted
    # history behind.
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    if not {'idx_tasks_user', 'idx_tasks_status', 'idx_tasks_created'} <= indexes:
        return None

    def table_info(table: str) -> tuple[tuple[object, ...], ...]:
        """Return the durable column shape, including defaults and PK flags.

        Matching names alone would let a hand-created table with compatible
        names but different nullability/defaults pass the legacy adoption
        gate.  The known pre-history schemas are small, so compare the full
        ``table_info`` signature before creating migration history.
        """
        return tuple(
            tuple(row)
            for row in conn.execute(f'PRAGMA table_info({table})')
        )

    def columns(table: str) -> set[str]:
        return {row[1] for row in table_info(table)}

    def index_columns(index: str) -> tuple[str, ...]:
        # ``PRAGMA index_info`` is used instead of trusting the index name:
        # an operator can restore a database with a stale or hand-created
        # index carrying the expected name but indexing another column.
        return tuple(
            row[0] for row in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (index,),
            )
        )

    def has_expected_indexes(with_priority: bool) -> bool:
        expected = {
            'idx_tasks_user': ('user_id',),
            'idx_tasks_status': ('status',),
            'idx_tasks_created': ('created_at',),
        }
        if with_priority:
            expected['idx_tasks_priority'] = ('priority',)
        return all(
            index_columns(name) == columns_
            for name, columns_ in expected.items()
        )

    v1_tasks = {
        'task_id', 'user_id', 'command', 'status', 'position', 'cpu', 'mem',
        'devices', 'stdout', 'stderr', 'returncode', 'timed_out', 'error',
        'created_at', 'started_at', 'finished_at', 'device_num', 'device_ids',
        'est_time', 'target_spec',
    }
    v1_sandboxes = {
        'name', 'cpu', 'mem', 'devices', 'cgroup_path', 'created_at', 'pids',
    }
    task_columns = columns('tasks')
    sandbox_columns = columns('sandboxes')

    # ``PRAGMA table_info`` rows are (cid, name, type, notnull, default, pk).
    # Keep the baseline explicit and ordered; SQLite preserves this shape for
    # the original 0001 tables and for the ALTER TABLE boundaries below.
    v1_task_info = (
        (0, 'task_id', 'TEXT', 0, None, 1),
        (1, 'user_id', 'TEXT', 1, None, 0),
        (2, 'command', 'TEXT', 1, None, 0),
        (3, 'status', 'TEXT', 1, "'queued'", 0),
        (4, 'position', 'INTEGER', 0, '0', 0),
        (5, 'cpu', 'INTEGER', 0, '0', 0),
        (6, 'mem', 'TEXT', 0, "'0'", 0),
        (7, 'devices', 'TEXT', 0, "'[]'", 0),
        (8, 'stdout', 'TEXT', 0, None, 0),
        (9, 'stderr', 'TEXT', 0, None, 0),
        (10, 'returncode', 'INTEGER', 0, None, 0),
        (11, 'timed_out', 'INTEGER', 0, '0', 0),
        (12, 'error', 'TEXT', 0, None, 0),
        (13, 'created_at', 'REAL', 0, None, 0),
        (14, 'started_at', 'REAL', 0, None, 0),
        (15, 'finished_at', 'REAL', 0, None, 0),
        (16, 'device_num', 'INTEGER', 1, '0', 0),
        (17, 'device_ids', 'TEXT', 1, "'[]'", 0),
        (18, 'est_time', 'INTEGER', 0, '0', 0),
        (19, 'target_spec', 'TEXT', 1, '\'{"type":"host"}\'', 0),
    )
    v1_sandbox_info = (
        (0, 'name', 'TEXT', 0, None, 1),
        (1, 'cpu', 'INTEGER', 0, '0', 0),
        (2, 'mem', 'TEXT', 0, "'0'", 0),
        (3, 'devices', 'TEXT', 0, "'[]'", 0),
        (4, 'cgroup_path', 'TEXT', 0, None, 0),
        (5, 'created_at', 'REAL', 0, None, 0),
        (6, 'pids', 'TEXT', 0, "'[]'", 0),
    )
    if (
        task_columns == v1_tasks
        and sandbox_columns == v1_sandboxes
        and table_info('tasks') == v1_task_info
        and table_info('sandboxes') == v1_sandbox_info
    ):
        return 1 if has_expected_indexes(False) else None
    if task_columns != v1_tasks | {'priority'}:
        return None
    # v2/v3 predate the sandbox lifecycle column; v4 includes it.  Keep the
    # boundary explicit so a half-applied 0004 cannot be mistaken for v3.
    state_info = v1_sandbox_info + (
        (7, 'state', 'TEXT', 1, "'ACTIVE'", 0),
    )
    priority_info = (20, 'priority', 'INTEGER', 1, '0', 0)
    has_state = (
        sandbox_columns == v1_sandboxes | {'state'}
        and table_info('sandboxes') == state_info
    )
    has_no_state = sandbox_columns == v1_sandboxes
    if has_no_state and table_info('sandboxes') != v1_sandbox_info:
        return None
    if (
        not (has_state or has_no_state)
        or table_info('tasks') != v1_task_info + (priority_info,)
        or not has_expected_indexes(True)
    ):
        return None
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()[0] or ''
    normalized_sql = re.sub(r'\s+', ' ', sql.lower())
    if re.search(r'priority\s*>=\s*0\s+and\s+priority\s*<=\s*1', normalized_sql):
        return 4 if has_state else 3
    if re.search(r'priority\s*>=\s*0', normalized_sql):
        return 2 if has_no_state else None
    return None


def _adopt_legacy_history(conn: sqlite3.Connection,
                          migrations: Sequence[Migration]) -> dict[int, sqlite3.Row]:
    version = _legacy_worker_version(conn)
    if version is None:
        raise MigrationError(
            "检测到未纳入迁移历史的旧数据库，请先执行独立的数据库割接"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_HISTORY_TABLE_SQL)
        now = datetime.now(timezone.utc).isoformat()
        for migration in migrations:
            if migration.version > version:
                break
            conn.execute(
                "INSERT INTO schema_migrations "
                "(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, now),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return _read_history(conn)


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
    """Validate the required shape before using a database.

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
            "数据库结构不符合要求："
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
    """Apply every pending migration to a database with migration history."""
    path = Path(database).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    migrations = discover_migrations(package)
    with _connect(path) as conn:
        tables = _user_tables(conn)
        history = _read_history(conn)

        if tables and "schema_migrations" not in tables:
            history = _adopt_legacy_history(conn, migrations)
        if (
            "schema_migrations" in tables
            and not history
            and tables != {"schema_migrations"}
        ):
            raise MigrationError(
                "数据库存在 schema_migrations 但没有任何记录，"
                "拒绝猜测旧 schema；请先执行独立的数据库割接"
            )
        elif "schema_migrations" not in tables and not history:
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


def restore_database(
    database: os.PathLike[str] | str,
    backup: os.PathLike[str] | str,
    backup_dir: os.PathLike[str] | str,
    role: str,
) -> Path | None:
    """Restore a SQLite backup atomically and retain a backup of the target.

    The caller must stop the service before invoking this function.  Both the
    input backup and the resulting database are checked with
    ``integrity_check``.  The returned path is the safety backup of the
    database that was replaced.
    """
    target_path = Path(database).expanduser().resolve()
    source_path = Path(backup).expanduser().resolve()
    if not source_path.is_file():
        raise MigrationError(f"备份数据库不存在: {source_path}")
    if target_path == source_path:
        raise MigrationError("恢复源不能与目标数据库相同")

    def integrity(path: Path) -> None:
        try:
            with _connect(path) as conn:
                result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise MigrationError(f"SQLite 数据库不可读: {path}: {exc}") from exc
        if result != "ok":
            raise MigrationError(f"SQLite integrity_check 失败: {path}: {result}")

    integrity(source_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    safety_backup = None
    if target_path.is_file():
        integrity(target_path)
        safety_backup = backup_database(target_path, backup_dir, role)

    temporary = target_path.with_name(
        f".{target_path.name}.restore-{os.getpid()}"
    )
    temporary.unlink(missing_ok=True)
    source = _connect(source_path)
    destination = sqlite3.connect(temporary)
    try:
        source.backup(destination)
        destination.commit()
        result = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            raise MigrationError(f"恢复后 SQLite integrity_check 失败: {result}")
    except sqlite3.DatabaseError as exc:
        destination.close()
        source.close()
        temporary.unlink(missing_ok=True)
        raise MigrationError(f"恢复 SQLite 数据库失败: {exc}") from exc
    except Exception:
        destination.close()
        source.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()
    try:
        # The service is required to be stopped. Remove sidecars before the
        # swap so a permission error cannot leave a successfully replaced DB
        # while reporting restore failure; no writer can recreate them in the
        # maintenance window.
        for suffix in ("-wal", "-shm"):
            Path(f"{target_path}{suffix}").unlink(missing_ok=True)
        if target_path.exists():
            temporary.chmod(target_path.stat().st_mode & 0o777)
        os.replace(temporary, target_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise MigrationError(f"原子替换数据库失败: {exc}") from exc
    return safety_backup
