# Neu Box — 轻量多节点资源管理与沙盒隔离

## 架构

```
浏览器 ──→ Master (Flask) ──→ Worker (Flask) ──→ Host / 已有 Docker 容器
              │                      │
              │ 节点发现/请求转发      │ cgroup v2 + eBPF 沙盒
              │ 每60s轮询节点状态      │ 命令队列 / 日志文件
           .env + config.json      .env
```

**WEB工作模式：**
- **命令模式**：Master 转发命令到 Worker，Worker 维护 FIFO 任务队列，在沙盒中执行 Host 或已有 Docker 容器命令。日志实时写入文件，前端全量拉取 + 进度条

**CLI工作模式：**
- **当前终端**：把当前 shell 加入独占设备沙盒。
- **Host 命令**：提交一次性 Host 命令并查询结果。
- **已有容器命令**：通过 `docker_existing` 在运行中的容器内执行命令，不改变容器生命周期。

## 运行

```bash
# Master
cd master
python main.py

# Worker（sudo 启动，自动复制 neu-sbox 到 /usr/local/bin）
cd worker
sudo python main.py
```

## CLI用法

管理当前 shell 的独占沙盒，或提交 Host、已有容器命令。Worker 通过 `/proc/<pid>/status` 校验 PID 归属，无需密码。

```bash
# 安装 — Worker 启动时自动复制到 /usr/local/bin/neu-sbox

# ── 沙盒（终端隔离） ──
neu-sbox acquire 1              # 申请 1 个 NPU，加入当前 shell
neu-sbox acquire 2 4 8          # 申请 2 NPU + 4 核 CPU + 8G 内存
neu-sbox acquire --devices 1,3  # 指定卡 1、3，加入当前 shell
neu-sbox acquire --devices 1 --pid 12345  # 将指定进程加入新沙盒
neu-sbox status                 # 查看当前 shell 是否在沙盒中
neu-sbox join sbx_pengyt_12345.slice  # 将当前 shell 加入已有沙盒（需归属校验）
neu-sbox list                   # 列出我的沙盒（显示设备卡号、CPU、内存）
neu-sbox release <name>         # 释放指定沙盒
# 已在沙盒中再次 acquire 会先销毁旧沙盒，再创建新沙盒

# ── 命令任务（一次性执行，类似前端命令模式） ──
neu-sbox acquire 1 2 4 "npu-smi info"     # 1 NPU + 2 核 + 4G 执行 Host 命令
neu-sbox acquire 0 4 8 "python train.py"  # 0 NPU + 4 核 + 8G 跑训练
neu-sbox tasks                              # 查看任务队列
neu-sbox result <task_id>                   # 查看任务结果和日志

# ── 已有容器命令 ──
neu-sbox acquire --devices 1 --container training-01 \
  --workdir /workspace --command "python train.py"
# 支持 --env、--workdir、--container-user
# 目标容器需已挂载所申请的 /dev/davinciN 或 /dev/nvidiaN

# ── 远程 Worker ──
export NEU_BOX_URL=http://<worker_ip>:59075
neu-sbox acquire 1
```

## 配置

### Master — `master/.env`

| 变量 | 默认值 | 含义 |
|---|---|---|
| `listen` | `0.0.0.0` | Master 监听地址 |
| `port` | `25565` | Master 监听端口 |
| `db_dir` | `./db` | SQLite 数据库目录（实验记录） |
| `poll_interval` | `15` | 节点状态轮询间隔（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `SECRET_KEY` | (随机) | Flask session 加密密钥，生产环境务必设置固定值 |
| `ADMIN_USER` | `admin` | 初始管理员用户名 |
| `ADMIN_PASS` | `admin` | 初始管理员密码 |
| `upload_max_size` | `10485760` | 实验图片上传大小限制（字节），默认 10MB |
| `EXP_LOG_DIR` | `./logs/exp` | 实验日志缓存目录 |

节点列表由 `master/config.json` 中的 `nodes_pool` 数组管理，支持前端 UI 动态增删：

| 字段 | 含义 |
|---|---|
| `nodes_pool[].name` | 节点显示名称 |
| `nodes_pool[].host` | Worker IP 地址 |
| `nodes_pool[].port` | Worker 端口 |

### Worker — `worker/.env`

| 变量 | 默认值 | 含义 |
|---|---|---|
| `port` | `59075` | Worker 监听端口 |
| `listen` | `0.0.0.0` | Worker 监听地址 |
| `cgroup_version` | `2` | cgroup 版本（1 或 2） |
| `device_filter` | — | 设备名正则过滤，如 `davinci[0-9]+`（NPU）或 `nvidia[0-9]+`（GPU） |
| `db_dir` | `./db` | SQLite 数据库目录 |
| `sandbox_reaper_interval` | `30` | 收尸线程扫描间隔（秒） |
| `command_timeout` | `0` | 命令执行超时（秒），0 = 不限制 |
| `command_max_completed` | `200` | 已完成任务保留上限 |
| `MAX_LOG_SIZE` | `2097152` | 单日志文件最大字节数（2MB），超出截断前半部 |
| `LOG_DIR` | `./logs/tasks` | 任务日志文件存储目录 |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `sandbox_script_path` | — | 沙盒管理脚本路径（cgroup + eBPF） |

## 数据流

### Web 命令模式

```
POST /command/run {node_id, user_id, command, cpu, memory, device_num, target}
  │
  Master ──→ Worker /command/run
               │
               └─ TaskQueue.submit() → 持久化到 SQLite → 返回 {task_id, position}
  │
TaskQueue 后台消费线程:
  ├─ 取队首任务
  ├─ SbxManager.allocate_sandbox(...)  → 分配设备并创建 sandbox
  ├─ host → Popen('bash -i -c <cmd>', ...) → 交互模式（自动source ~/.bashrc）
  ├─ docker_existing → 暂停 Docker Exec → host PID 加入 cgroup → 继续执行
  ├─ 后台线程逐块 read() stdout        → 实时写入 {LOG_DIR}/{task_id}.log
  ├─ 进程结束                          → DB 更新状态/返回码
  └─ SbxManager.destroy_sandbox()      → cgroup.freeze → cgroup.kill → 销毁

日志存储: 文件系统（非 SQLite），前端通过 XHR + 进度条全量拉取
GET /command/result/<id>/log?raw=1  → 纯文本日志 + Content-Length 头
```

### 终端沙盒模式 (`neu-sbox`)

```
POST /sandbox/acquire {username, pid, device_num, cpu, memory}
  → /proc/<pid>/cgroup 检测是否已在沙盒中 → 是: 先销毁旧 sandbox
  → /proc/<pid>/status 校验归属 → 创建 sbx_{user}_{pid}.slice + 设备分配 → PID 加入
POST /sandbox/join {username, pid, sandbox_name}
  → /proc/<pid>/status 校验 PID 归属 → sandbox 名称校验 owner → 加入目标 cgroup
POST /command/run {user_id, command, device_ids/device_num, target}
  → TaskQueue 持久化并排队 → 创建 sandbox cgroup
  → target=host → 在 Host 执行命令
  → target=docker_existing → 暂停 Docker Exec → host PID 移入 sandbox → 继续执行
  → 保存日志、状态和退出码 → 销毁 sandbox
POST /sandbox/release {sandbox_name}
  → destroy_sandbox() → cgroup.freeze → cgroup.kill → 设备归还
GET  /sandbox/list
  → 返回活跃 sandbox 及其设备和进程
```

### 沙盒销毁流程

```
destroy_sandbox(name)
  └─ sandbox.sh destroy
       ├─ cgroup.freeze = 1    冻结 cgroup 内所有进程
       ├─ cgroup.kill = 1      内核全杀（无竞态）
       ├─ rmdir 整棵 cgroup；失败则保留数据库记录
       └─ 删除仍属于当前 cgroup 的 BPF 独占条目
```

## Master 用户系统

Master 内置统一的用户登录认证。首次启动自动创建管理员账号（默认 `admin`/`admin`，通过 `ADMIN_USER` / `ADMIN_PASS` 环境变量修改）。

- 登录后可为每个节点保存命令任务用户名，选中节点时自动填入
- 未登录无法调用任何写操作 API（命令提交、实验管理、节点增删等）
- 节点状态查询等只读接口保持公开，前端无需登录即可看到节点列表
- `neu-sbox` CLI 直连 Worker，完全不受 Master 认证影响

## 公网暴露安全注意事项

如需将 Master 暴露到公网，建议以下防护措施：

- TLS 终止（Nginx/Caddy 反代 + Let's Encrypt 证书）
- 登录接口速率限制（`limit_req` 防暴力破解）
- `SECRET_KEY` 设为强随机值（`python -c "import secrets; print(secrets.token_hex(32))"`）
- Session cookie 配置 `secure=True, httponly=True, samesite=Strict`
- Worker 节点仅监听内网地址，不对外暴露端口
- Master ↔ Worker 之间走 VPN/内网专线（如 WireGuard、Tailscale）
- **推荐方案**：不直接暴露 Master，改用 VPN 接入内网后访问

## 前端功能

| 功能 | 说明 |
|------|------|
| 日志查看 | XHR 全量拉取 + 进度条，自动滚底，`\r` 进度条处理 |
| 任务重跑 | Host 任务右侧 `↻` 按钮，确认后以原参数重新提交 |
| 执行目标 | 支持 Host 和已有 Docker 容器 |
| 实验记录 | 保存时复制日志副本（>500KB 截断），展开时懒加载；`\r` 处理 |
| 节点管理 | 前端 UI 增删节点，60s 自动轮询 |
