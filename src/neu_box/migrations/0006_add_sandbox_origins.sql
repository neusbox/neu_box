-- join 进来的进程"原来在哪个 cgroup"（JSON: {pid: cgroup 路径}）。
-- release 时据此把它们还回去；没有记录的进程是沙盒里长出来的，随沙盒收掉。
ALTER TABLE sandboxes ADD COLUMN origins TEXT NOT NULL DEFAULT '{}';
