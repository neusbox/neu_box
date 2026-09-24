# 数据库迁移手册

Worker 拥有独立的 SQLite 数据库及迁移序列（WebUI 的 master 数据库迁移由 neu_box_webui 仓库维护，机制相同）：

```text
src/neu_box/migrations/
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
- `checksum` 是迁移文件完整内容的 SHA-256，存成 `sha256:<hex>`；
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

`0004_add_sandbox_state.sql` 为 sandbox 增加 `CREATING`、`ACTIVE`、
`DESTROYING` 生命周期。存量记录迁移为 `ACTIVE`，新建 sandbox 则从
`CREATING` 开始。

`0005_create_containers.sql` 增加 `containers` 表，记录容器归属：容器的
mount namespace 是主键，同时是 BPF `container_owner` map 的 key；
`init_start_time` 用于判断登记对应的进程是否还在（挡住 PID 复用）。

`0006_add_sandbox_origins.sql` 给 sandboxes 增加 `origins`（JSON：
`{pid: cgroup 路径}`），记录从外面借进沙盒的进程（acquire 借出去的那个
终端）原本在哪个 cgroup，release 时据此把它们还回去；没有记录的进程是沙盒里
长出来的，随沙盒一起收掉。

必须解析 JSON 或执行复杂数据转换时，可以使用 Python（例如未来的 `0008`）：

```text
0008_normalize_task_target.py
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

这样重启恢复可以清理尚未落库的带 label 容器；已有容器记录和 Docker target
任务会在迁移时回填。纯 Host sandbox 保持 `0`，释放时不依赖 Docker daemon。

迁移接收由运行器管理的同一个 connection。不得调用 `commit()`、`rollback()`、`BEGIN`、`SAVEPOINT` 或 `executescript()`；运行器使用 SQLite authorizer 阻止迁移自行控制事务。一个版本的结构变化、数据变化和迁移记录在同一事务内提交，失败时整个版本回滚。

发布构建会把迁移 `.py` 同时作为 PyInstaller 数据资源和动态导入模块收集，因此运行器
既能读取原始字节计算 checksum，也能执行 `upgrade()`；打包测试会校验这两个入口，
新增 Python 迁移不需要手工维护 hidden-import 清单。

## 开发流程

一次 schema 修改应同时完成：

1. 在对应角色的 `migrations/` 增加下一个连续版本文件；
2. 更新数据库模块中的 `REQUIRED_COLUMNS` / `REQUIRED_INDEXES`，让启动和 `db check`
   校验迁移后的完整结构；旧数据库的割接由独立的升级脚本负责，不在运行时代码中猜测
   或自动接管；
3. 更新 CRUD 代码；
4. 为新迁移补测试，至少覆盖新库和上一版本数据库升级，并验证数据保留；
5. 提升项目版本，构建新 RPM；
6. 不再修改已经进入 RPM 的旧迁移。

本地手动检查：

```bash
neuboxctl --config /path/to/worker.env db status
neuboxctl --config /path/to/worker.env db migrate
neuboxctl --config /path/to/worker.env db check
neuboxctl --config /path/to/worker.env db backup \
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
执行 `neuboxctl sandbox list`、启动服务、健康检查。
首个 RPM 的一次性交割脚本还会先在备份副本上试迁移，再迁移正式数据库。

RPM 不提供数据库自动降级，也不在 scriptlet 中恢复业务数据。迁移或健康检查失败
时，应保持 Worker 停止，由部署流程用升级前的一致备份恢复数据库，再安装明确的
旧 RPM。恢复会丢弃升级后产生的新写入，因此只能在上游仍停止派发的维护窗口执行。

备份必须使用 SQLite backup API，不能只复制主 `.db` 文件；WAL 模式下直接复制单个文件可能遗漏尚未 checkpoint 的数据。

0001_initial.sql
```sql
CREATE TABLE tasks (
    task_id     TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,                  -- Linux 用户名，不是 users 表外键
    command     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    position    INTEGER DEFAULT 0,              -- 当前排队位置，仅 queued 状态有效
    cpu         INTEGER DEFAULT 0,
    mem         TEXT DEFAULT '0',
    devices     TEXT DEFAULT '[]',
    stdout      TEXT,
    stderr      TEXT,
    returncode  INTEGER,
    timed_out   INTEGER DEFAULT 0,
    error       TEXT,
    created_at  REAL,
    started_at  REAL,
    finished_at REAL,
    device_num  INTEGER NOT NULL DEFAULT 0,
    device_ids  TEXT NOT NULL DEFAULT '[]',
    est_time    INTEGER DEFAULT 0,
    target_spec TEXT NOT NULL DEFAULT '{"type":"host"}'
);

CREATE INDEX idx_tasks_user ON tasks(user_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_created ON tasks(created_at);

CREATE TABLE sandboxes (
    name        TEXT PRIMARY KEY,
    cpu         INTEGER DEFAULT 0,
    mem         TEXT DEFAULT '0',
    devices     TEXT DEFAULT '[]',
    cgroup_path TEXT,
    created_at  REAL,
    pids        TEXT DEFAULT '[]'
);
```

0002_add_task_priority.sql
```sql
ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 CHECK (priority >= 0);
CREATE INDEX idx_tasks_priority ON tasks(priority);
```

0003_tighten_task_priority.sql
```sql
-- 0003: 收紧 priority 取值范围到 0..1（0=普通、1=赶论文）
-- SQLite 无法给已有列追加 CHECK 约束，采用 暂存 → 删列 → 加列 → 回填 → 清理：
--   1. 暂存现有 (task_id, priority)
--   2. 先删依赖该列的索引，再删列
--   3. 以 0..1 的 CHECK 重新加列
--   4. 回填历史值；此前 API 允许 >1 的值，回填时钳制为 1
--   5. 清理暂存表并重建索引

CREATE TABLE _tasks_priority_staging AS
    SELECT task_id, priority FROM tasks;

DROP INDEX idx_tasks_priority;
ALTER TABLE tasks DROP COLUMN priority;
ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0
    CHECK (priority >= 0 AND priority <= 1);

UPDATE tasks
    SET priority = COALESCE(
        (SELECT MIN(s.priority, 1) FROM _tasks_priority_staging s
         WHERE s.task_id = tasks.task_id),
        0
    );

DROP TABLE _tasks_priority_staging;
CREATE INDEX idx_tasks_priority ON tasks(priority);
```

0004_add_sandbox_state.sql
```sql
ALTER TABLE sandboxes ADD COLUMN state TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (state IN ('CREATING', 'ACTIVE', 'DESTROYING'));
```
