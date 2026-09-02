# 数据库迁移手册

Worker 拥有独立的 SQLite 数据库及迁移序列（WebUI 的 master 数据库迁移由 neu_box_webui 仓库维护，机制相同）：

```text
src/neu_box/worker/migrations/
```

应用启动不创建表、执行 `ALTER TABLE` 或吞掉迁移异常。建表和历史数据转换只发生
在显式 `db migrate` 中；RPM scriptlet 不运行数据库迁移，部署流程必须显式调用。

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
ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 CHECK (priority >= 0);
CREATE INDEX idx_tasks_priority ON tasks(priority);
```

`0003_tighten_task_priority.sql` 收紧 priority 到 0..1。SQLite 无法给已有列追加
CHECK 约束，所以采用 暂存数据 → 删索引 → 删列 → 以新 CHECK 重新加列 → 回填
（历史值 >1 钳制为 1）→ 重建索引 的方式完成。

必须解析 JSON 或执行复杂数据转换时，可以使用 Python：

```text
0004_normalize_task_target.py
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

发布构建会把迁移 `.py` 同时作为 PyInstaller 数据资源和动态导入模块收集，因此运行器
既能读取原始字节计算 checksum，也能执行 `upgrade()`；打包测试会校验这两个入口，
新增 Python 迁移不需要手工维护 hidden-import 清单。

## 开发流程

一次 schema 修改应同时完成：

1. 在对应角色的 `migrations/` 增加下一个连续版本文件；
2. 更新数据库模块中的 `REQUIRED_COLUMNS` / `REQUIRED_INDEXES`，让启动和 `db check` 能发现实际 schema 损坏；
   例外：迁移只是**新增**列或索引时，不要把它们加入必需集合——旧库 baseline
   接管的校验发生在新迁移执行**之前**，要求新字段会让所有存量旧库被拒绝接管。
   迁移本身会给所有库（含 legacy）补齐对象，代价只是启动校验发现不了该对象丢失；
3. 更新 CRUD 代码；
4. 增加迁移测试，至少覆盖新库和上一版本数据库升级，并验证数据保留；
5. 运行单元测试；
6. 提升项目版本，构建新 RPM；
7. 不再修改已经进入 RPM 的旧迁移。

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

`serve` 只接受 `state=current` 且所需表、字段、索引完整的数据库。它不会为了“先启动起来”自动修表。

## 首次 baseline

`0001_initial.sql` 是新数据库的唯一初始结构。第一次把已有 Neu Box 数据库纳入迁移系统时：

- 数据库没有 `schema_migrations`；
- 运行器检查当前角色要求的全部表、字段和索引；
- 检查通过后只登记 `0001_initial`，不会重新执行建表 SQL，也不会删除额外的历史字段或表；
- 缺少任何必需对象时停止，不猜测数据库来自哪个旧版本；
- 如果业务表已经存在但有人创建了空的 `schema_migrations`，同样停止，防止绕过 baseline 检查。

旧版本到当前结构的转换必须写明确、可测试的兼容迁移，不能恢复成运行时 `try/except ALTER TABLE`。

## 部署与恢复

普通 RPM 升级的顺序是：停止上游派发并排空 Worker、停止服务、创建 SQLite 一致
备份、清理空闲的旧 BPF 状态、安装新 RPM、显式执行 `db migrate` 和 `db check`、
执行 `neu-box sandbox list`、启动服务、健康检查。
首个 RPM 的一次性交割脚本还会先在备份副本上试迁移，再迁移正式数据库。

RPM 不提供数据库自动降级，也不在 scriptlet 中恢复业务数据。迁移或健康检查失败
时，应保持 Worker 停止，由部署流程用升级前的一致备份恢复数据库，再安装明确的
旧 RPM。恢复会丢弃升级后产生的新写入，因此只能在上游仍停止派发的维护窗口执行。

备份必须使用 SQLite backup API，不能只复制主 `.db` 文件；WAL 模式下直接复制单个文件可能遗漏尚未 checkpoint 的数据。
