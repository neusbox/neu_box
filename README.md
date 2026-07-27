# Neu Box — 轻量多节点资源管理与沙盒隔离

## 架构

```
浏览器 ──→ Master (Flask) ──→ Worker (Flask) ──→ ttyd / bash
              │                      │
              │ 节点发现/请求转发      │ cgroup v2 + eBPF 沙盒
              │ 每60s轮询节点状态      │ 端口池管理
              │                      │ 终端 / 命令队列 / 日志文件
           .env + config.json      .env
```

**两种工作模式：**
- **终端模式**：Master 转发请求到 Worker，Worker 启动 ttyd（终端 over HTTP），前端通过 iframe 嵌入。中栏显示当前节点活跃终端和沙盒列表
- **命令模式**：Master 转发命令到 Worker，Worker 维护 FIFO 任务队列，逐任务在沙盒中执行。日志实时写入文件，前端全量拉取 + 进度条

## 运行

```bash
# Master
cd master
python main.py

# Worker（sudo 启动，自动安装 neu-sbox.sh 到 /etc/profile.d/）
cd worker
sudo python main.py
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
| `HOST_IP` | (自动检测) | Worker 对外 IP，终端 URL 中返回 |
| `port_pool_start` | `59081` | ttyd 端口池起始 |
| `port_pool_end` | `59100` | ttyd 端口池结束 |
| `cgroup_version` | `2` | cgroup 版本（1 或 2） |
| `device_filter` | — | 设备名正则过滤，如 `davinci[0-9]+`（NPU）或 `nvidia[0-9]+`（GPU） |
| `db_dir` | `./db` | SQLite 数据库目录 |
| `sandbox_reaper_interval` | `30` | 收尸线程扫描间隔（秒） |
| `command_timeout` | `0` | 命令执行超时（秒），0 = 不限制 |
| `command_max_completed` | `200` | 已完成任务保留上限 |
| `MAX_LOG_SIZE` | `2097152` | 单日志文件最大字节数（2MB），超出截断前半部 |
| `LOG_DIR` | `./logs/tasks` | 任务日志文件存储目录 |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `dev_info_script_path` | — | 设备状态采集脚本路径，返回 `{"total":N,"idle":N}` |
| `sandbox_script_path` | — | 沙盒管理脚本路径（cgroup + eBPF） |

## 数据流

### 终端模式

```
POST /terminal/create {node_id, username, password, cpu, memory, device_num}
  │
  Master ──→ Worker /terminal/create
               │
               ├─ Port_Pool.acquire_port()         → 分配端口
               ├─ Popen(ttyd -p <port> -q ...)      → 启动终端服务
               ├─ SbxManager.allocate_for_terminal() → 创建 cgroup 沙盒
               └─ 返回 {terminal_url, sandbox_id}
  │
浏览器 iframe.src = terminal_url  →  ttyd WebSocket
  │
用户关闭标签页 → ttyd -q 自动退出
  │
Reaper(每30s) → cleanup_orphaned():
  ├─ PID已死 → 销毁沙盒 + 归还端口
  └─ PID存活但端口无ESTABLISHED连接 → SIGTERM → 销毁沙盒 + 归还端口
```

### 命令模式

```
POST /command/run {node_id, user_id, command, cpu, memory, device_num}
  │
  Master ──→ Worker /command/run
               │
               └─ TaskQueue.submit() → 持久化到 SQLite → 返回 {task_id, position}
  │
TaskQueue 后台消费线程:
  ├─ 取队首任务
  ├─ SbxManager.create_sandbox()       → 创建 cgroup 沙盒
  ├─ Popen('bash -i -c <cmd>', ...)    → 交互模式（自动source ~/.bashrc）
  ├─ 后台线程逐块 read() stdout        → 实时写入 {LOG_DIR}/{task_id}.log
  ├─ 进程结束                          → DB 更新状态/返回码
  └─ SbxManager.destroy_sandbox()      → cgroup.freeze → cgroup.kill → 销毁

日志存储: 文件系统（非 SQLite），前端通过 XHR + 进度条全量拉取
GET /command/result/<id>/log?raw=1  → 纯文本日志 + Content-Length 头
```

### 终端沙盒模式 (`neu-sbox`)

将当前 shell 加入独占设备的 cgroup 沙盒，或提交一次性命令任务。Worker 通过 `/proc/<pid>/status` 校验 PID 归属，无需密码。

```bash
# 安装 — Worker 用 sudo 启动时自动安装到 /etc/profile.d/
source /etc/profile.d/neu-sbox.sh

# ── 沙盒（终端隔离） ──
neu-sbox acquire 1              # 申请 1 个 NPU，加入当前 shell
neu-sbox acquire 2 4 8          # 申请 2 NPU + 4 核 CPU + 8G 内存
neu-sbox status                 # 查看当前 shell 是否在沙盒中
neu-sbox list                   # 列出我的沙盒（显示设备卡号、CPU、内存）
neu-sbox release <name>         # 释放指定沙盒
# 已在沙盒中再次 acquire → 自动释放旧沙盒，覆盖为新资源

# ── 命令任务（一次性执行，类似前端命令模式） ──
neu-sbox acquire 1 2 4 "nvidia-smi"       # 1 NPU + 2 核 + 4G 执行 nvidia-smi
neu-sbox acquire 0 4 8 "python train.py"  # 0 NPU + 4 核 + 8G 跑训练
neu-sbox tasks                              # 查看任务队列
neu-sbox result <task_id>                   # 查看任务结果和日志
```

```bash
# 远程 Worker
export NEU_BOX_URL=http://<worker_ip>:59075
neu-sbox acquire 1
```

```
POST /sandbox/acquire {username, pid, device_num, cpu, memory}
  → /proc/<pid>/cgroup 检测是否已在沙盒中 → 是: 先释放旧沙盒
  → /proc/<pid>/status 校验归属 → cgroup 创建 + 设备分配 → PID 加入
POST /sandbox/acquire {..., command: "nvidia-smi"}   # 带命令 → 走任务队列
  → 类似 POST /command/run，提交后返回 task_id
POST /sandbox/release {sandbox_name}
  → destroy_sandbox() → cgroup.freeze → cgroup.kill → 设备归还
GET  /sandbox/list
  → 返回活跃沙盒详情（名称、CPU、内存、设备列表、PID）
```

### 沙盒销毁流程

```
destroy_sandbox(name)
  └─ sandbox.sh destroy
       ├─ cgroup.freeze = 1    冻结 cgroup 内所有进程
       ├─ cgroup.kill = 1      内核全杀（无竞态）
       ├─ 清理 BPF map 设备预留
       └─ rmdir cgroup
```

## Master 用户系统

Master 内置统一的用户登录认证。首次启动自动创建管理员账号（默认 `admin`/`admin`，通过 `ADMIN_USER` / `ADMIN_PASS` 环境变量修改）。

- 登录后可配置每个节点的凭据（节点名 → 用户名 + 密码），操作时自动填入，无需反复输入
- 未登录无法调用任何写操作 API（终端创建、命令提交、实验管理、节点增删等）
- 节点状态查询等只读接口保持公开，前端无需登录即可看到节点列表
- `neu-sbox` CLI 直连 Worker，完全不受 Master 认证影响
- Worker 侧零改动

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
| 任务重跑 | completed/failed 任务右侧 `↻` 按钮，确认后以原参数重新提交 |
| 实验记录 | 保存时复制日志副本（>500KB 截断），展开时懒加载；`\r` 处理 |
| 终端面板 | 终端模式中栏显示活跃终端（用户、设备卡号、资源）和命令沙盒 |
| 节点管理 | 前端 UI 增删节点，60s 自动轮询 |
