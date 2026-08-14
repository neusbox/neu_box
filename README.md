# Neu Box — 轻量多节点资源管理与沙盒隔离

## 架构

```
浏览器 ──→ Master (Flask) ──→ Worker (Flask) ──→ Host / 已有 Docker 容器
              │                      │
              │ 节点发现/请求转发      │ cgroup v2 + eBPF 沙盒
              │ 每60s轮询节点状态      │ 命令队列 / 日志文件
          master.env + nodes.json      worker.env
                 （均位于 /etc/neu-box）
```

**WEB工作模式：**

- **命令模式**：Master 转发命令到 Worker，Worker 维护 FIFO 任务队列，在沙盒中执行 Host 或已有 Docker 容器命令。日志实时写入文件，前端全量拉取 + 进度条

**CLI工作模式：**

- **当前终端**：把当前 shell 加入独占设备沙盒。
- **Host 命令**：提交一次性 Host 命令并查询结果。
- **已有容器命令**：通过 `docker_existing` 在运行中的容器内执行命令，不改变容器生命周期。

## 打包、部署与运行

项目发布为三个自包含 Linux 程序：

- `neu-box-master`：Master 服务与 Master 数据库管理命令；
- `neu-box-worker`：Worker 服务与 Worker 数据库管理命令；
- `neu-box-install`：安装、升级、备份、迁移、健康检查和回滚入口。

Master 和 Worker 发布程序包含 Python 解释器及 Python 依赖，目标机器不需要安装 Python、pip 或 uv。Worker 仍依赖宿主机的 cgroup v2、systemd、Bash、`bpftool`、`busctl` 和设备驱动；Docker 执行目标还需要 Docker daemon。

```bash
# 安装依赖并测试
UV_CACHE_DIR=/tmp/neu-box-uv-cache \
  uv sync --frozen --all-extras --all-groups
UV_CACHE_DIR=/tmp/neu-box-uv-cache \
  uv run --frozen pytest -q

# 构建当前机器架构的发布包
UV_CACHE_DIR=/tmp/neu-box-uv-cache \
  uv run --frozen --all-extras --group build \
  python deploy/build_release.py
```

构建机需要 clang 来预编译 eBPF 对象。PyInstaller 不支持直接跨架构构建，amd64 和 arm64 应分别在对应架构、且不新于目标机 glibc 的 Linux 环境中构建。

```bash
# 校验并解压；版本和架构以实际产物为准
cd dist
sha256sum -c neu-box-0.1.0-linux-arm64.tar.gz.sha256
tar -xzf neu-box-0.1.0-linux-arm64.tar.gz
cd neu-box-0.1.0-linux-arm64

# 计算节点安装 Worker；默认安装后立即启动
sudo ./neu-box-install install --role worker

# Master 节点安装 Master；Master 和 Worker 可以安装在同一台机器
sudo ./neu-box-install install --role master
```

需要先修改配置时，安装命令添加 `--no-start`，修改 `/etc/neu-box/*.env` 后再通过 systemd 启动。每次命令只安装一个明确角色。

```bash
# 服务管理
sudo systemctl start neu-box-master neu-box-worker
sudo systemctl stop neu-box-master neu-box-worker
systemctl status neu-box-master neu-box-worker
journalctl -u neu-box-worker -f

# 使用新发布包升级对应角色
sudo ./neu-box-install upgrade --role worker
sudo ./neu-box-install upgrade --role master

# 查看安装状态或回滚程序和升级前数据库
sudo neu-box-install status
sudo neu-box-install rollback
```

安装器会校验发布包、备份 SQLite、在数据库副本上试跑迁移、切换版本、启动服务并检查 `/healthz`。当前 `neu-sbox` 客户端随 Worker 安装，仍需要 Bash、curl 和 Python 3。

| 路径 | 内容 |
|---|---|
| `/opt/neu-box/releases/<version>/` | 不可变的版本化程序 |
| `/opt/neu-box/current` | 当前版本原子符号链接 |
| `/etc/neu-box/` | Master/Worker 配置与节点列表 |
| `/var/lib/neu-box/` | SQLite、任务日志、上传文件和实验日志 |
| `/var/log/neu-box/` | Master/Worker 运行日志 |
| `/var/backups/neu-box/` | 升级和回滚备份 |
| `/usr/local/sbin/neu-box-install` | 当前版本安装器 |
| `/usr/local/bin/neu-sbox` | 指向当前 Worker 版本客户端的链接 |

完整的首次安装、旧部署导入、NPU/GPU 配置、升级和回滚流程见 [部署与升级手册](docs/deployment.md)。数据库迁移规则见 [数据库迁移手册](docs/database-migrations.md)。

## 开发流程

本项目的开发机器同时承担生产服务，因此不支持从源码启动 Master 或 Worker。源码工作区只用于修改代码、运行无特权单元测试和构建发布包；服务、数据库和硬件调试统一使用安装后的版本化二进制。

```text
修改源码
  → 运行 pytest
  → 提升为未使用过的版本号
  → 构建并校验发布包
  → neu-box-install install/upgrade
  → 在真实服务上验证
  → 失败则 rollback，修复后构建下一个版本
```

- 不执行 `uv run neu-box-master serve` 或 `uv run neu-box-worker serve`；源码进程可能读取生产 `/etc/neu-box` 配置。
- 不并行启动第二个 Worker；所有 Worker 实例共享宿主机的 `/sys/fs/cgroup`、`/sys/fs/bpf` 和真实设备。
- 单元测试和构建可以在生产服务运行时执行；需要 sudo 或真实硬件的集成测试必须进入维护窗口，并针对已安装的发布包运行。
- 已经构建用于部署调试的版本号不得复用，避免同一 release 路径出现不同内容。

## CLI用法

管理当前 shell 的独占沙盒，或提交 Host、已有容器命令。Worker 通过 `/proc/<pid>/status` 校验 PID 归属，无需密码。

```bash
# neu-box-install 安装 Worker 时创建 /usr/local/bin/neu-sbox

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

### Master — `/etc/neu-box/master.env`

| 变量 | 默认值 | 含义 |
|---|---|---|
| `NEU_BOX_LISTEN` | `0.0.0.0` | Master 监听地址 |
| `NEU_BOX_PORT` | `25565` | Master 监听端口 |
| `NEU_BOX_HTTP_THREADS` | `8` | Waitress HTTP 线程数 |
| `NEU_BOX_POLL_INTERVAL` | `15` | 节点状态轮询间隔（秒） |
| `NEU_BOX_DB_PATH` | `/var/lib/neu-box/master/master.db` | SQLite 数据库文件 |
| `NEU_BOX_NODES_CONFIG` | `/etc/neu-box/nodes.json` | Worker 节点列表 |
| `NEU_BOX_UPLOAD_DIR` | `/var/lib/neu-box/master/uploads` | 实验图片目录 |
| `NEU_BOX_EXPERIMENT_LOG_DIR` | `/var/lib/neu-box/master/experiment-logs` | 实验日志副本目录 |
| `NEU_BOX_LOG_DIR` | `/var/log/neu-box` | 服务日志目录 |
| `NEU_BOX_BACKUP_DIR` | `/var/backups/neu-box` | 数据库备份目录 |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `SECRET_KEY` | 安装时生成 | Flask session 加密密钥 |
| `ADMIN_USER` | `admin` | 初始管理员用户名 |
| `ADMIN_PASS` | `admin` | 初始管理员密码 |
| `NEU_BOX_UPLOAD_MAX_SIZE` | `10485760` | 实验图片上传大小限制（字节），默认 10MB |

节点列表由 `/etc/neu-box/nodes.json` 中的 `nodes_pool` 数组管理，支持前端 UI 动态增删：

| 字段 | 含义 |
|---|---|
| `nodes_pool[].name` | 节点显示名称 |
| `nodes_pool[].host` | Worker IP 地址 |
| `nodes_pool[].port` | Worker 端口 |

### Worker — `/etc/neu-box/worker.env`

| 变量 | 默认值 | 含义 |
|---|---|---|
| `NEU_BOX_PORT` | `59075` | Worker 监听端口 |
| `NEU_BOX_LISTEN` | `0.0.0.0` | Worker 监听地址 |
| `NEU_BOX_HTTP_THREADS` | `8` | Waitress HTTP 线程数 |
| `NEU_BOX_DEVICE_FILTER` | `davinci[0-9]+` | 设备名完整匹配正则，GPU 可设为 `nvidia[0-9]+` |
| `NEU_BOX_DB_PATH` | `/var/lib/neu-box/worker/neu_box.db` | SQLite 数据库文件 |
| `NEU_BOX_TASK_LOG_DIR` | `/var/lib/neu-box/worker/task-logs` | 任务日志目录 |
| `NEU_BOX_LOG_DIR` | `/var/log/neu-box` | 服务日志目录 |
| `NEU_BOX_BACKUP_DIR` | `/var/backups/neu-box` | 数据库备份目录 |
| `NEU_BOX_SANDBOX_SCRIPT` | `/opt/neu-box/current/share/neu-box/sandbox/v2/sandbox.sh` | 沙盒管理脚本 |
| `NEU_BOX_DEVICE_INFO_SCRIPT` | — | 可选设备状态脚本，GPU 节点使用 |
| `NEU_BOX_SANDBOX_REAPER_INTERVAL` | `30` | 收尸线程扫描间隔（秒） |
| `NEU_BOX_COMMAND_TIMEOUT` | `0` | 命令执行超时（秒），0 = 不限制 |
| `NEU_BOX_COMMAND_MAX_COMPLETED` | `200` | 已完成任务保留上限 |
| `NEU_BOX_COMMAND_QUEUE_RECENT` | `200` | 状态接口返回的近期任务上限 |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

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
  ├─ 后台线程逐块 read() stdout        → 实时写入 {NEU_BOX_TASK_LOG_DIR}/{task_id}.log
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
