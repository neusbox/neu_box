# 数据库迁移手册

Master 和 Worker 各自拥有 SQLite 数据库及迁移序列：

```text
src/neu_box/master/migrations/
src/neu_box/worker/migrations/
```

应用启动不再创建表、执行 `ALTER TABLE` 或吞掉迁移异常。建表和历史数据转换只发生在显式 `db migrate` 中；正式部署由安装器自动调用。

## 迁移记录表

每个数据库包含：

```sql
CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
```

- `version` 是四位文件名前缀对应的整数；
- `name` 来自文件名；
- `checksum` 是迁移文件完整内容的 SHA-256；
- `applied_at` 是 UTC ISO 8601 时间。

迁移必须从 `0001` 开始且连续。已发布的迁移文件不可修改；程序发现名称或 checksum 与数据库记录不一致时会拒绝启动或升级。

## SQL 与 Python 迁移

普通 DDL、索引和简单数据修改使用 SQL：

```text
0002_add_task_priority.sql
```

```sql
ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_tasks_priority ON tasks(priority);
```

必须解析 JSON 或执行复杂数据转换时，可以使用 Python：

```text
0003_normalize_task_target.py
```

```python
import json


def upgrade(conn):
    rows = conn.execute("SELECT task_id, target_spec FROM tasks").fetchall()
    for task_id, raw in rows:
        value = json.loads(raw)
        conn.execute(
            "UPDATE tasks SET target_spec=? WHERE task_id=?",
            (json.dumps(value, separators=(",", ":")), task_id),
        )
```

迁移接收由运行器管理的同一个 connection。不得调用 `commit()`、`rollback()`、`BEGIN`、`SAVEPOINT` 或 `executescript()`；运行器使用 SQLite authorizer 阻止迁移自行控制事务。一个版本的结构变化、数据变化和迁移记录在同一事务内提交，失败时整个版本回滚。

## 开发流程

一次 schema 修改应同时完成：

1. 在对应角色的 `migrations/` 增加下一个连续版本文件；
2. 更新数据库模块中的 `REQUIRED_COLUMNS` / `REQUIRED_INDEXES`，让启动和 `db check` 能发现实际 schema 损坏；
3. 更新 CRUD 代码；
4. 增加迁移测试，至少覆盖新库和上一版本数据库升级，并验证数据保留；
5. 运行单元测试；
6. 提升项目版本，构建新发布包；
7. 不再修改已经进入发布包的旧迁移。

```bash
UV_CACHE_DIR=/tmp/neu-box-uv-cache uv run --frozen pytest -q
```

本地手动检查：

```bash
neu-box-worker --config /path/to/worker.env db status
neu-box-worker --config /path/to/worker.env db migrate
neu-box-worker --config /path/to/worker.env db check
neu-box-worker --config /path/to/worker.env db backup \
  --output-dir /path/to/backups
```

Master 命令相同，只需把程序名替换为 `neu-box-master`。

`serve` 只接受 `state=current` 且所需表、字段、索引完整的数据库。它不会为了“先启动起来”自动修表。

## 首次 baseline

`0001_initial.sql` 是新数据库的唯一初始结构。第一次把已有 Neu Box 数据库纳入迁移系统时：

- 数据库没有 `schema_migrations`；
- 运行器检查当前角色要求的全部表、字段和索引；
- 检查通过后只登记 `0001_initial`，不会重新执行建表 SQL，也不会删除额外的历史字段或表；
- 缺少任何必需对象时停止，不猜测数据库来自哪个旧版本；
- 如果业务表已经存在但有人创建了空的 `schema_migrations`，同样停止，防止绕过 baseline 检查。

旧版本到当前结构的转换必须写明确、可测试的兼容迁移，不能恢复成运行时 `try/except ALTER TABLE`。

## 部署与回滚

安装器的数据库顺序是：停止服务、SQLite 一致备份、备份副本试迁移、正式迁移、启动新服务、健康检查。这样在没有独立测试线的实验室环境中，至少会用真实生产库副本验证迁移。

不提供通用自动 downgrade SQL。新版本失败或管理员显式回滚时，安装器恢复升级前数据库备份，再切回旧程序。显式回滚会丢弃升级后产生的新写入，所以必须在维护窗口内确认。

备份必须使用 SQLite backup API，不能只复制主 `.db` 文件；WAL 模式下直接复制单个文件可能遗漏尚未 checkpoint 的数据。

