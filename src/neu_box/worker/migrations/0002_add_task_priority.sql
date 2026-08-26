ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0 CHECK (priority >= 0);
CREATE INDEX idx_tasks_priority ON tasks(priority);
