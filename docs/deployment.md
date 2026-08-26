# 部署与升级手册

本文描述 Neu Box 版本化发布包的构建、首次安装、旧部署导入、升级和回滚流程。所有命令都从已解压发布目录执行；安装器不直接读取 tar 包。

## 发布模型

一个发布包同时包含 Master 和 Worker，但每台机器只安装需要的角色。程序放在 `/opt/neu-box/releases/<version>`，`/opt/neu-box/current` 原子指向当前版本。配置和数据位于 `/etc`、`/var/lib`，不进入 release 目录。

安装器执行以下固定流程：

1. 校验 manifest、CPU 架构和包内每个文件的 SHA-256；
2. 将发布内容复制到新的不可变 release 目录；
3. 保留已有配置，创建缺失的稳定目录和模板；
4. 记录服务原状态并停止已安装角色；
5. 使用 SQLite backup API 备份数据库并执行完整性检查，同时备份配置和将被替换的文件；
6. 在数据库备份副本上试跑新版本迁移；
7. 迁移正式数据库并原子切换 `current`；
8. 安装 systemd unit，启用并启动服务；
9. 校验 `/healthz` 返回的角色和程序版本；
10. 安装当前版本的 `neu-box-install` 和 Worker 客户端链接，最后原子写入安装状态。

任何一步失败，安装器都会停止新服务、恢复数据库、`current`、unit、客户端和安装器，并恢复原来的服务启用/运行状态。失败的 release 目录可能保留，便于诊断，但不会成为 `current`。

## 目标机要求

共同要求：

- 64 位 Linux，发布包架构必须匹配 `amd64` 或 `arm64`；
- systemd；
- 目标机 glibc 不低于构建环境所使用的 glibc；
- 安装、升级和回滚使用 root 权限。

Worker 额外要求：

- cgroup v2，存在 `/sys/fs/cgroup/cgroup.controllers`；
- Bash、`bpftool`、`busctl`；
- 正确的 NPU/GPU 驱动和 `/dev` 设备节点；
- 使用 Docker 目标时安装并运行 Docker daemon。

发布包已携带 Python 解释器、Python 包、预编译 eBPF 对象和静态
`neu-sbox` Go 客户端。Worker 运行不需要目标机安装 Python、pip、uv、clang
或 Go；挂载进容器的客户端也不依赖 Bash、curl、Python 或动态链接库。

## 构建发布包

项目版本只在 `src/neu_box/__init__.py` 中维护。发布过的版本号不能复用；安装器会拒绝内容不同但版本号相同的包。
构建机需要 Python/uv、clang 和 Go；这些工具都不是目标节点的运行依赖。

```bash
# 安装依赖并测试
UV_CACHE_DIR=/tmp/neu-box-uv-cache \
  uv sync --frozen --all-extras --all-groups
UV_CACHE_DIR=/tmp/neu-box-uv-cache \
  uv run --frozen pytest -q

# 测试无运行时依赖的 Go 客户端
(cd client/neu-sbox && go test ./...)

# 构建当前机器架构的发布包
UV_CACHE_DIR=/tmp/neu-box-uv-cache \
  uv run --frozen --all-extras --group build \
  python deploy/build_release.py
```

构建会：

- 用锁文件中的依赖分别生成 Master/Worker PyInstaller `onedir`；
- 生成单文件安装器；
- 用 clang 编译当前源码对应的 `device_block.o`，同时放入 Worker 程序和共享资源；
- 以 `CGO_ENABLED=0` 构建当前发布架构的静态 `neu-sbox`；
- 写入 `manifest.json`、完整的 `SHA256SUMS`、tar.gz 及 tar 包 checksum。

PyInstaller 产物不能直接跨 CPU 架构构建。amd64、arm64 应分别在对应架构构建；为提高兼容性，应使用不新于最老目标机的 Linux/glibc 构建环境。

验证并解包：

```bash
sha256sum -c neu-box-0.1.2-linux-amd64.tar.gz.sha256
tar -xzf neu-box-0.1.2-linux-amd64.tar.gz
cd neu-box-0.1.2-linux-amd64
```

## 首次安装

### Worker

NPU 节点默认配置使用 `davinci[0-9]+`：

```bash
sudo ./neu-box-install install --role worker
```

GPU 节点建议先不启动，修改配置后再启动：

```bash
sudo ./neu-box-install install --role worker --no-start
sudo editor /etc/neu-box/worker.env
sudo systemctl start neu-box-worker
curl --noproxy '*' http://127.0.0.1:59075/healthz
```

GPU 配置至少调整：

```dotenv
NEU_BOX_DEVICE_FILTER=nvidia[0-9]+
NEU_BOX_DEVICE_INFO_SCRIPT=/opt/neu-box/current/share/neu-box/info/gpu_info.sh
```

### Master

```bash
sudo ./neu-box-install install --role master --no-start
sudo editor /etc/neu-box/master.env
sudo editor /etc/neu-box/nodes.json
sudo systemctl start neu-box-master
curl --noproxy '*' http://127.0.0.1:25565/healthz
```

安装器会生成随机 `SECRET_KEY`。首次启动前应修改默认 `ADMIN_PASS`；它只用于创建尚不存在的管理员，不会在以后重置已有密码。

同一台机器需要同时运行 Master 和 Worker 时，分别执行两次安装：

```bash
sudo ./neu-box-install install --role worker --no-start
sudo ./neu-box-install install --role master --no-start
sudo systemctl start neu-box-worker neu-box-master
```

每次命令只安装一个明确角色。

## 配置参考

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
| `NEU_BOX_DEVICE_INFO_SCRIPT` | NPU 脚本路径 | 设备状态脚本；根据设备厂商显式配置 |
| `NEU_BOX_SANDBOX_REAPER_INTERVAL` | `30` | 收尸线程扫描间隔（秒） |
| `NEU_BOX_COMMAND_TIMEOUT` | `0` | 命令执行超时（秒），0 = 不限制 |
| `NEU_BOX_COMMAND_MAX_COMPLETED` | `200` | 已完成任务保留上限 |
| `NEU_BOX_COMMAND_QUEUE_RECENT` | `200` | 状态接口返回的近期任务上限 |
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |

### 设备分配与外部占用

空闲设备 = 受管设备 − 沙盒已分配（worker DB）− 沙盒外占用（`NEU_BOX_DEVICE_INFO_SCRIPT` 实时查询）。外部占用检测**每次分配前**同步执行，因此 vLLM、docker 等沙盒外进程占用的卡不会被分配；沙盒内任务释放后设备立即回到空闲池。

设备状态脚本查询失败/超时/返回 `total=0` 时，worker **沿用上一次成功查询的结果**（fail-closed），而不是视为"全部空闲"——否则系统高负载下 npu-smi 卡死时，所有外部占用的卡会被误判为空闲并重新分配，造成双占用。

注意：外部进程与 neu-box 任务之间不存在内核级互斥，两者同时申请同一张卡时先到先得（例如任务分配后 vLLM 才 attach 到该卡）。共享节点上建议所有设备使用方都通过 neu-box 申请设备，使 worker 成为唯一的分配仲裁者。

## 从源码部署导入

legacy 参数只允许在对应角色第一次 `install` 时使用，并且 `--role` 必须明确为一个角色。所有路径都必须是绝对路径。同一台机器原来同时运行 Master 和 Worker 时，先后执行两次带 `--no-start` 的单角色安装，分别导入各自数据，最后再统一启动新服务。

Worker 示例：

```bash
sudo ./neu-box-install install --role worker --no-start \
  --legacy-config /srv/old-neu-box/worker/.env \
  --legacy-database /srv/old-neu-box/worker/db/neu_box.db
```

Master 示例：

```bash
sudo ./neu-box-install install --role master --no-start \
  --legacy-config /srv/old-neu-box/master/.env \
  --legacy-database /srv/old-neu-box/master/db/master.db \
  --legacy-nodes /srv/old-neu-box/master/config.json
```

旧 `.env` 中的监听地址、端口和业务参数会转换到新配置；旧 checkout 下的数据库、日志和脚本路径不会照搬。数据库通过 SQLite backup API 复制，而不是直接复制可能存在 WAL 的文件。目标数据库如果已经存在，安装器会停止并要求人工确认，不会静默跳过导入。

导入的旧数据库必须符合当前已知 schema。安装器严格检查所需表、字段和索引后登记 baseline；未知或残缺结构会在正式数据库变化前失败。先保留旧源码部署，直到新服务和数据验证完毕。

旧源码部署使用的 `neu_box_master.service`、`neu_box_worker.service` 不属于新安装器的管理对象。第一次切换时在维护窗口里直接停止并禁用旧服务，再运行上述安装命令；新版本只管理 `neu-box-master.service`、`neu-box-worker.service`。这是一项一次性部署操作，不在产品代码中保留旧服务探测或接管逻辑。

## 升级

Worker 升级前应安排维护窗口，停止提交新任务并确认队列中没有必须保留的运行任务。安装器会停止服务，但不会替你等待业务任务排空。

```bash
tar -xzf neu-box-0.2.2-linux-amd64.tar.gz
cd neu-box-0.2.2-linux-amd64
sudo ./neu-box-install upgrade --role worker
```

Master 使用 `--role master`。如果同一台机器已经安装多个角色，由于它们共享一个 `current`，所有已安装角色会一起升级。

配置模板只在文件不存在时复制；升级不会自动合并新配置项。新版本增加配置时，应阅读 release notes，把需要的项加入现有 `/etc/neu-box/*.env`。

查看结果：

```bash
sudo neu-box-install status
systemctl status neu-box-worker
journalctl -u neu-box-worker -n 100 --no-pager
curl --noproxy '*' http://127.0.0.1:59075/healthz
```

## 回滚

```bash
sudo neu-box-install rollback
```

回滚会切换到上一版本，并恢复升级前数据库。升级成功后产生的新数据会丢失，因此交互模式会要求确认；自动化调用必须显式添加 `--yes`。

安装状态只维护一个可直接回滚的上一版本。回滚成功后，刚才的版本和回滚前救援备份会成为新的上一状态，因此可以再次回切。配置升级时从不自动覆盖，所以普通回滚不会替换当前配置；每次操作前的配置副本仍保留在 `/var/backups/neu-box`。

## 目录、权限与日志

安装后的主要路径：

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

关键路径的默认权限：

| 路径 | 默认所有者/用途 |
|---|---|
| `/etc/neu-box/master.env` | `root:neu-box`, `0640` |
| `/etc/neu-box/nodes.json` | `neu-box:neu-box`, `0640`，Master UI 可修改 |
| `/var/lib/neu-box/master` | `neu-box:neu-box` |
| `/var/lib/neu-box/worker` | root，Worker 服务以 root 运行 |
| `/var/backups/neu-box` | root，`0700` |

Master 使用无登录系统用户 `neu-box`；Worker 因 cgroup、eBPF、设备检测及用户切换需要以 root 运行。

应用同时写 journald 和轮转文件：

```bash
journalctl -u neu-box-master -f
journalctl -u neu-box-worker -f
tail -f /var/log/neu-box/worker.log
```

任务输出位于 `/var/lib/neu-box/worker/task-logs`，不是服务日志。安装器当前不自动清理旧 release 或备份；确认稳定后由管理员按实验室保留策略清理，但必须保留安装状态引用的上一 release 和数据库备份。

## 无特权发布演练

`--root` 和 `--no-systemd` 只用于测试安装器，不用于正式部署：

```bash
./neu-box-install --root /tmp/neu-box-stage --no-systemd \
  install --role worker --no-start
./neu-box-install --root /tmp/neu-box-stage --no-systemd \
  install --role master --no-start
./neu-box-install --root /tmp/neu-box-stage --no-systemd status
```

绝对运行路径会映射到测试 root，安装器拒绝包含 `..` 的逃逸路径。此模式仍会真实执行发布包内的数据库命令，但不会调用 systemd 或 HTTP 健康检查。
