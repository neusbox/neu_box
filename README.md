# Neu Box

Neu Box 是实验室内部使用的多节点加速设备管理服务。Master 提供 Web 界面、用户与实验记录；Worker 在计算节点上管理命令队列、cgroup v2 沙盒和设备独占。

```text
浏览器 ──> Master ──HTTP──> Worker ──> Host / 已运行的 Docker 容器
                         └─> cgroup v2 + eBPF 设备隔离
```

项目现在采用标准 Python 包结构开发，并发布为三个自包含 Linux 程序：

- `neu-box-master`：Master 服务与 Master 数据库管理命令；
- `neu-box-worker`：Worker 服务与 Worker 数据库管理命令；
- `neu-box-install`：统一安装、升级、备份、迁移、健康检查和回滚入口。

Master 和 Worker 的发布程序包含 Python 解释器及 Python 依赖，目标机器不需要安装 Python、pip 或 uv。Worker 仍依赖宿主机的 cgroup v2、systemd、`bpftool`、`busctl` 和设备驱动；使用 Docker 执行目标时还需要 Docker daemon。当前 `neu-sbox` 终端客户端仍是随版本安装的 Bash 客户端，需要 Bash、curl 和 Python 3。

## 安装发布包

发布包按 CPU 架构生成，例如 `neu-box-0.1.0-linux-amd64.tar.gz` 或 `neu-box-0.1.0-linux-arm64.tar.gz`。

```bash
sha256sum -c neu-box-0.1.0-linux-arm64.tar.gz.sha256
tar -xzf neu-box-0.1.0-linux-arm64.tar.gz
cd neu-box-0.1.0-linux-arm64

# 计算节点
sudo ./neu-box-install install --role worker

# Master 节点
sudo ./neu-box-install install --role master
```

安装器会校验发布包内全部文件，初始化稳定目录，自动执行数据库迁移，安装并启动 systemd 服务，然后请求 `/healthz`。已有配置和持久化数据不会被模板覆盖。

从旧的源码运行方式首次切换时，需要显式提供旧配置和数据库；Master 还应提供原节点配置。完整命令及 NPU/GPU 配置见 [部署与升级手册](docs/deployment.md)。

## 稳定目录

| 路径 | 内容 |
|---|---|
| `/opt/neu-box/releases/<version>/` | 不可变的版本化程序 |
| `/opt/neu-box/current` | 当前版本原子符号链接 |
| `/etc/neu-box/` | Master/Worker 配置与节点列表 |
| `/var/lib/neu-box/` | SQLite、任务日志、上传文件和实验日志 |
| `/var/log/neu-box/` | Master/Worker 运行日志 |
| `/var/backups/neu-box/` | 升级和回滚前的数据库、配置及被替换文件 |
| `/usr/local/sbin/neu-box-install` | 当前版本安装器 |
| `/usr/local/bin/neu-sbox` | 指向当前 Worker 版本客户端的链接 |

程序、配置和数据彼此分离。升级只增加新的 release 目录并切换 `current`，不会从源码 checkout 或当前工作目录读取运行文件。

## 日常运维

```bash
# 服务状态与日志
systemctl status neu-box-master
systemctl status neu-box-worker
journalctl -u neu-box-worker -f

# 安装状态
sudo neu-box-install status

# 数据库状态、完整性检查与手动备份
/opt/neu-box/current/master/neu-box-master \
  --config /etc/neu-box/master.env db status
/opt/neu-box/current/worker/neu-box-worker \
  --config /etc/neu-box/worker.env db check
/opt/neu-box/current/worker/neu-box-worker \
  --config /etc/neu-box/worker.env db backup

# 回滚程序及升级前数据库
sudo neu-box-install rollback
```

升级时，使用新发布包里的安装器：

```bash
sudo ./neu-box-install upgrade --role worker
```

安装器先用 SQLite backup API 创建一致备份，再在备份副本上试跑迁移。新服务未通过健康检查时，它会自动切回旧程序、恢复数据库和原有外部文件，并恢复升级前的服务状态。显式回滚会恢复升级前数据库，因此会先要求确认。

数据库迁移的格式、开发流程和 baseline 规则见 [数据库迁移手册](docs/database-migrations.md)。

## `neu-sbox` 客户端

Worker 安装后可直接查看完整帮助：

```bash
neu-sbox help
```

常见用法：

```bash
neu-sbox acquire --devices 1
neu-sbox acquire --device-num 2 --cpu 4 --mem 8
neu-sbox status
neu-sbox list
neu-sbox join <sandbox_name>
neu-sbox release <sandbox_name>

# 提交一次性 Host 命令
neu-sbox acquire --device-num 1 --command "npu-smi info"
neu-sbox tasks
neu-sbox result <task_id>

# 远程 Worker
export NEU_BOX_URL=http://worker-host:59075
```

已有 Docker 容器命令通过 `--container` 提交；目标容器必须已经运行并挂载所申请的设备。容器生命周期不由 Neu Box 管理。

## 开发

uv 只用于开发、锁定依赖和构建，不会安装到目标服务器。

```bash
uv sync --all-extras --all-groups --frozen
UV_CACHE_DIR=/tmp/neu-box-uv-cache uv run --frozen pytest -q
```

本地启动前必须显式迁移临时数据库：

```bash
mkdir -p /tmp/neu-box-dev
cp deploy/config/nodes.json.example /tmp/neu-box-dev/nodes.json

export NEU_BOX_DB_PATH=/tmp/neu-box-dev/master.db
export NEU_BOX_NODES_CONFIG=/tmp/neu-box-dev/nodes.json
uv run neu-box-master db migrate
uv run neu-box-master serve --listen 127.0.0.1
```

构建当前机器架构的发布包：

```bash
UV_CACHE_DIR=/tmp/neu-box-uv-cache \
  uv run --frozen --all-extras --group build \
  python deploy/build_release.py
```

构建机还需要 clang 来预编译 eBPF 对象。PyInstaller 不支持直接跨架构构建，因此 amd64 和 arm64 应分别在对应架构、且不新于目标机 glibc 的 Linux 构建环境中执行。构建结果写入 `dist/`。

源码布局：

```text
src/neu_box/
├── database/        # 共享迁移运行器和数据库 CLI
├── master/          # Master app、API、服务、静态资源和迁移
└── worker/          # Worker app、执行器、运行资源和迁移
deploy/              # 构建、安装器、配置模板和 systemd units
tests/unit/          # 无特权单元与发布流程测试
```

更多细节见 [部署与升级手册](docs/deployment.md)；线上 HTTP/硬件集成测试仍保留在 `tests/`，不会被默认的单元测试命令自动执行。
