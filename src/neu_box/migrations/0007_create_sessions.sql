-- acquire 会话的账本。
--
-- 与 tasks 的区别：会话不是一次性任务，它"拿到卡之后一直占着，直到 release"，
-- 所以状态机是 queued → allocating → active → released，旁路 cancelled/failed。
-- 中间态照记（要能看"排了多久、占了多久"），但**运行时逻辑不依赖它**：调度看的是
-- 内存队列 + 沙盒/cgroup 实时状态。进程异常退出后只做"修表"——把没有收尾的行标成
-- interrupted，不重建请求、不重跑校验。
--
-- sandbox_name 是**软指针**（故意不加 FOREIGN KEY）：sandboxes 行在沙盒销毁时是
-- 真删的，历史行指向一个已经消失的沙盒是正常现象，当时用了几张卡看 devices 快照。
CREATE TABLE sessions (
    request_id   TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    device_num   INTEGER NOT NULL DEFAULT 0,
    device_ids   TEXT NOT NULL DEFAULT '[]',
    priority     INTEGER NOT NULL DEFAULT 0
        CHECK (priority >= 0 AND priority <= 1),
    state        TEXT NOT NULL
        CHECK (state IN ('queued', 'allocating', 'active', 'released',
                         'cancelled', 'failed', 'interrupted')),
    sandbox_name TEXT,
    devices      TEXT NOT NULL DEFAULT '[]',
    code         TEXT,
    requested_at REAL NOT NULL,
    acquired_at  REAL,
    finished_at  REAL
);

CREATE INDEX idx_sessions_state ON sessions(state);
CREATE INDEX idx_sessions_sandbox ON sessions(sandbox_name);
