-- 容器归属记录: 容器不持有授权，只是某个沙盒的受托方。
-- mount_namespace 同时是 BPF container_owner map 的 key；init_start_time
-- 用于判断这条记录的仓库是否还在（挡住 PID 复用）。
CREATE TABLE containers (
    mount_namespace INTEGER PRIMARY KEY,
    container_ref TEXT NOT NULL,
    container_id TEXT NOT NULL DEFAULT '',
    init_host_pid INTEGER NOT NULL,
    init_start_time INTEGER NOT NULL,
    sandbox_name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (state IN ('ACTIVE', 'DESTROYING')),
    created_at REAL NOT NULL
);
CREATE INDEX idx_containers_sandbox ON containers(sandbox_name);
