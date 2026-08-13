CREATE TABLE tasks (
    task_id     TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    command     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    position    INTEGER DEFAULT 0,
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

