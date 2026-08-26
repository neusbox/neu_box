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
