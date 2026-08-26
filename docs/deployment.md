# 部署与升级手册（worker）

## 三仓库结构

2026-08-25 起 Neu Box 拆分为三个独立维护、独立发版的仓库：

| 仓库 | 角色 | 版本 | 部署 |
|---|---|---|---|
| **neu_box**（本仓库） | worker：节点侧设备沙盒 + 任务执行 | 0.3.0+ | `/opt/neu-box/releases/<v>` + `current`（安装器 `neu-box-install`） |
| [neu_box_webui](https://github.com/nihaopeng/neu_box_webui) | WebUI：节点池 / 转发 / 实验记录 | 0.0.1+ | `/opt/neu-box/webui/releases/<v>`（install.sh，无安装器） |
| [neu_box_goClient](https://github.com/nihaopeng/neu_box_goClient) | `neu-sbox` Go 客户端（直连 worker） | 0.0.1+ | `/usr/local/bin/neu-sbox`（install.sh，静态二进制） |

三者只通过 HTTP 契约相交（worker API 见 [worker-api.md](worker-api.md)，
WebUI API 见 webui 仓库 docs/master-api.md）。兼容矩阵用本仓库的
submodule 指针（`webui/`、`goClient/`）表达：指针指向的提交即该版本
已验证的配套版本。

## 发布模型

- 发布包：`neu-box-<version>-linux-<arch>.tar.gz` + `.sha256`
- 包内容：`worker/`（PyInstaller 可执行目录）、`neu-box-install`（安装器）、
  `config/worker.env.example`、`systemd/neu-box-worker.service`、
  `share/neu-box/{sandbox,info}`（沙盒脚本/设备状态脚本/BPF 对象）、
  `docs/`、`manifest.json`、`SHA256SUMS`（逐文件校验和）
- 版本钉死：同一版本号不同内容**拒绝安装**（`/opt/neu-box/releases/<v>`
  存在且 SHA256SUMS 不一致 → 报错，要求换版本号）
- 每个版本独立目录 + `current` 符号链接；升级失败自动回滚
  （程序、数据库、配置、systemd unit 快照恢复）

## 目标机要求

- Linux x86_64 / arm64，systemd
- Python 运行时已打包进发布物（PyInstaller），无需系统 Python
- root（安装与升级需要）
- 设备驱动与工具链（如 `npu-smi` / `nvidia-smi`）由驱动方提供
- Docker 客户端（仅容器执行目标需要）

## 构建发布包

```bash
uv sync                      # 安装依赖（含 build 组 PyInstaller）
uv run pytest tests/unit     # 单测
uv run deploy/build_release.py
# → dist/neu-box-<version>-linux-<arch>.tar.gz + .sha256
```

## 首次安装

```bash
tar -xzf neu-box-<v>-linux-<arch>.tar.gz
cd neu-box-<v>-linux-<arch>
sha256sum -c neu-box-<v>-linux-<arch>.tar.gz.sha256   # 从远端下载后
sudo ./neu-box-install install --role worker
```

安装器：校验 SHA256SUMS → 释放到 `/opt/neu-box/releases/<v>/` → 链
`current` → 生成 `/etc/neu-box/worker.env`（已存在则保留）→ 安装
`neu-box-worker.service` 并启动 → 健康检查（`/healthz`）。

## 配置参考 — `/etc/neu-box/worker.env`

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
| `NEU_BOX_DEVICE_INFO_SCRIPT` | NPU 脚本路径 | 设备状态脚本；根据设备厂商显式配置 |
| `NEU_BOX_SANDBOX_REAPER_INTERVAL` | `30` | 收尸线程扫描间隔（秒） |
| `NEU_BOX_COMMAND_TIMEOUT` | `0` | 命令执行超时（秒），0 = 不限制 |
| `NEU_BOX_COMMAND_MAX_COMPLETED` | `200` | 已完成任务保留上限 |
| `NEU_BOX_COMMAND_QUEUE_RECENT` | `200` | 状态接口返回的近期任务上限 |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

### 设备分配与外部占用

空闲设备 = 受管设备 − 沙盒已分配（worker DB）− 沙盒外占用
（`NEU_BOX_DEVICE_INFO_SCRIPT` 实时查询）。外部占用检测**每次分配前**
同步执行，因此 vLLM、docker 等沙盒外进程占用的卡不会被分配。

设备状态脚本查询失败/超时/返回 `total=0` 时，worker **沿用上一次成功
查询的结果**（fail-closed），而不是视为"全部空闲"——否则系统高负载下
npu-smi 卡死时，所有外部占用的卡会被误判为空闲并重新分配。

注意：外部进程与 neu-box 任务之间不存在内核级互斥，两者同时申请同一张卡
时先到先得。共享节点上建议所有设备使用方都通过 neu-box 申请设备。

## 从旧源码部署导入

legacy 参数只允许在 worker 第一次 `install` 时使用：

```bash
sudo ./neu-box-install install --role worker --no-start \
  --legacy-config /srv/old-neu-box/worker/.env \
  --legacy-database /srv/old-neu-box/worker/db/neu_box.db
```

## 升级

```bash
cd 解包目录
sudo ./neu-box-install upgrade --role worker
```

流程：校验 → 释放新版本 → 备份数据库/配置/被替换文件 → 停服务 →
运行迁移 → 切 `current` → 装 unit → 启动 → 健康检查。任一步失败自动
恢复升级前状态（服务、数据库、程序、符号链接）。

## 回滚

```bash
sudo ./neu-box-install rollback          # 交互确认
sudo ./neu-box-install rollback --yes    # 非交互
```

回滚到 `previous` 记录指向的版本（数据库从升级前快照恢复，新版本数据
保留在 `/var/backups/neu-box/<timestamp>-before-rollback/` 供前滚）。

## 目录、权限与日志

```
/opt/neu-box/releases/<v>/    每个版本独立目录（不可变）
/opt/neu-box/current          → releases/<当前版本>
/etc/neu-box/worker.env       运行时配置（升级不覆盖）
/var/lib/neu-box/worker/      数据库 + 任务日志
/var/log/neu-box/             服务日志
/var/backups/neu-box/         升级前备份（自动，按时间戳）
```

## API 版本

`/healthz` 与 `/status` 上报 `api_version`（当前 `1`）。仅破坏性变更
（删字段、改语义）时 +1；新增字段/端点不升版本。WebUI 与 Go 客户端
据此做兼容性检查（见各自仓库文档）。

## e2e 集成测试

`tests/test_queue.py` 需要已部署的 worker + WebUI：

```bash
NEU_BOX_TEST_MASTER=http://<master>:25565 \
NEU_BOX_TEST_PASS='<master 管理员密码>' \
uv run pytest tests/test_queue.py
```
