# 部署与升级手册（Worker）

## 三仓库边界

Neu Box 的三个仓库独立维护、独立发版，只通过 HTTP 契约协作：

| 仓库 | 职责 | 部署方式 |
|---|---|---|
| **neu_box**（本仓库） | 节点侧 Worker、设备沙盒与任务执行 | 架构相关 `neu-box-worker` RPM |
| [neu_box_webui](https://github.com/neusbox/neu_box_webui) | 节点池、转发、实验记录与 Web 界面 | Python 3.11+ 源码运行 |
| [neu_box_goClient](https://github.com/neusbox/neu_box_goClient) | 直连 Worker 的 `neu-sbox` 客户端 | Go 静态二进制 |

Worker API 见 [worker-api.md](worker-api.md)。本仓库通过 `thirds/webui/` 和
`thirds/goClient/` 固定已验证的配套提交。

## RPM 发布模型

发布物是当前构建架构的二进制 RPM，包名为
`neu-box-worker-<version>-<release>.<arch>.rpm`。RPM 管理以下内容：

```text
/usr/libexec/neu-box/worker/                 PyInstaller Worker bundle
/usr/libexec/neu-box/neu-box-sandbox        C++17 沙盒 CLI
/usr/libexec/neu-box/device_block.o          预编译 BPF 对象
/usr/share/neu-box/info/                     GPU/NPU 状态脚本
/usr/sbin/neu-box                            安装后的管理 CLI
/usr/sbin/neu-box-worker                     Worker 入口符号链接
/usr/lib/systemd/system/neu-box-worker.service
/etc/neu-box/worker.env                      %config(noreplace)
/var/lib/neu-box/worker/                     Worker 数据目录
/usr/share/licenses/neu-box-worker/LICENSE   许可证
```

RPM scriptlet 只在写入或删除程序文件前拒绝仍处于 active 状态的 Worker；安装后
best-effort 执行 `daemon-reload`，完整卸载时 disable service。它不负责排空任务、
检查 cgroup/BPF 状态、迁移数据库或验证部署结果，也不会启用或启动 Worker。

安装后的运维入口是 `/usr/sbin/neu-box`。源码仓库的 `run.sh` 只提供 `build`、
`test` 和 `deployment-test`，不用于已安装系统的生命周期管理。

## 数据安全边界

RPM 只管理 Worker。它不迁移、不移动、不删除：

- `/var/lib/neu-box/master/**`，包括 `experiment-logs/**` 和 `uploads/**`；
- `/etc/neu-box/master.env`、`/etc/neu-box/nodes.json`；
- WebUI 文件和任何外部配置的 experiment 目录；
- `/var/lib/neu-box/worker/task-logs/**`。

RPM 安装 vendor unit 到 `/usr/lib/systemd/system`，其中直接声明
`RequiresMountsFor=/var/lib/neu-box`。task logs 继续留在原目录，RPM 不复制、不改写
其内容。

## 目标机要求

- Linux `x86_64` 或 `aarch64`，使用 systemd 与 cgroup v2；
- Linux 5.7+，内核启用 cgroup device BPF（子 cgroup 身份归一依赖
  `bpf_get_current_ancestor_cgroup_id`）；
- root 或可用的 `sudo`；
- 对应厂商的驱动与状态工具，例如 `npu-smi` 或 `nvidia-smi`；
- `/bin/bash`（安装后的管理 CLI 使用）；仅使用容器执行目标时需要 Docker。

Python 运行时已包含在 PyInstaller bundle 中。生产 RPM 默认将 libbpf 静态链接
进沙盒程序；libelf/zstd/zlib 由 RPM 根据 ELF 动态依赖声明。目标机不需要
`bpftool`、`libbpf.so`、`busctl`、Clang、CMake 或 Python。

## 构建 RPM

构建机需要 `uv`、PyInstaller、CMake 3.20+、C++17 编译器、支持 BPF target 的
Clang、`pkg-config`、`rpmbuild`、提供 `readelf` 的 binutils，以及
libbpf 静态库和 libelf/zstd/zlib 开发库。
生产包应在最低支持版本、相同架构的构建系统上生成。

```bash
uv sync --frozen --all-groups
./run.sh test
./run.sh build
# → dist/rpm/neu-box-worker-<version>-<release>.<arch>.rpm

rpm -qpi dist/rpm/neu-box-worker-*.rpm
rpm -qpl dist/rpm/neu-box-worker-*.rpm
rpm -qpR dist/rpm/neu-box-worker-*.rpm
```

`./run.sh build` 调用 `deploy/build_release.py`，依次构建 native sandbox、
PyInstaller Worker bundle 和 RPM。默认静态链接 libbpf；
`--dynamic-libbpf` 只适合开发构建，并会自动把 RPM Release 标成 `.dev`；该包
不得签名或用于交割。RPM 封装器默认检查并拒绝含 `libbpf.so` 动态依赖的 sandbox，
显式开发封装也必须使用 `.dev` Release。完整构建会在同一个 native build 目录中
生成 sandbox 与 BPF object；封装器会执行 sandbox `--help` 自检并核对目标架构。
只重新封装生产产物时可执行：

```bash
python3 deploy/rpm/build_rpm.py
```

默认中间产物位于 `build/release/`，最终 RPM 位于 `dist/rpm/`。BPF 对象只在
构建机编译，目标机直接加载 RPM 中的 `device_block.o`。

正式发布还必须在构建后由发布流程签名 RPM，并在目标节点导入对应公钥；摘要校验
不能替代签名。

## 首次安装

RPM 安装后不会自动启动服务。先检查并修改配置，再显式初始化：

```bash
sudo dnf install ./neu-box-worker-<version>-<release>.<arch>.rpm
sudo systemctl daemon-reload
sudoedit /etc/neu-box/worker.env
sudo neu-box setup
curl -fsS http://127.0.0.1:59075/healthz
```

`neu-box setup` 依次执行 `db migrate`、`db check`，然后
`neu-box sandbox list`，最后执行 `systemctl enable --now
neu-box-worker.service` 并确认 systemd 报告 active。`sandbox list` 检查 BPF object
以及当前 cgroup、pin 和 attachment 状态；`/healthz` 确认 Worker 可以响应，并上报
版本和数据库 schema。

常用命令：

```bash
neu-box version
sudo neu-box status
sudo neu-box start                 # 也支持 stop/restart
sudo neu-box logs
sudo neu-box db status
sudo neu-box db backup --output-dir /var/backups/neu-box
sudo neu-box db migrate
sudo neu-box db check
```

## 普通 RPM 升级

Worker 当前没有 maintenance/drain 模式。升级前必须由上游停止向该节点派发新任务，
并等待现有任务自然结束。不要用停止服务代替排空：重启会把原先处于 running 的任务
标记为失败。

维护窗口中的固定顺序是：

1. 停止上游派发；
2. 确认 `GET /tasks` 中没有 `queued`/`running`，并运行
   `sudo neu-box sandbox list` 确认没有活跃沙盒；`GET /status` 中的
   `active_sandboxes` 只用于展示，不能替代 CLI 验收；
3. 停止 Worker，并再次确认没有残留进程、sandbox cgroup 或设备预留；
4. 使用 Worker CLI 创建一致的 SQLite 备份；
5. 执行 `neu-box sandbox cleanup`，卸载空闲的旧 BPF program 与 pins；
6. 用本地 RPM 执行 `dnf upgrade`；
7. 使用新 Worker 执行 `db migrate` 和 `db check`；
8. 执行 `neu-box sandbox list`，再启动服务并检查 active、版本、schema 和
   `/healthz`；
9. 检查通过后才恢复上游派发。

对应命令：

```bash
sudo systemctl stop neu-box-worker.service
sudo neu-box db backup --output-dir /var/backups/neu-box
sudo neu-box sandbox cleanup
sudo dnf upgrade ./neu-box-worker-<version>-<release>.<arch>.rpm
sudo systemctl daemon-reload
sudo neu-box db migrate
sudo neu-box db check
sudo neu-box sandbox list >/dev/null
sudo systemctl start neu-box-worker.service
sudo systemctl is-active neu-box-worker.service
neu-box version
sudo neu-box db status
curl -fsS http://127.0.0.1:59075/healthz
```

RPM 不自动备份数据库、运行 schema 迁移、清理 BPF 或重启服务，因此这些步骤不能
省略。RPM 本身不会替部署流程检查残留的 `sandbox_*` cgroup 或 Neu Box BPF pins；
必须在执行 `dnf` 前完成上面的显式验收和 cleanup。

## 配置参考

`/etc/neu-box/worker.env` 由 RPM 以 `%config(noreplace)` 管理：已有本地配置不会被
普通升级静默覆盖。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `NEU_BOX_PORT` | `59075` | Worker 监听端口 |
| `NEU_BOX_LISTEN` | `0.0.0.0` | Worker 监听地址 |
| `NEU_BOX_HTTP_THREADS` | `8` | Waitress HTTP 线程数 |
| `NEU_BOX_DEVICE_FILTER` | `davinci[0-9]+` | 设备名完整匹配正则；GPU 可设为 `nvidia[0-9]+` |
| `NEU_BOX_DB_PATH` | `/var/lib/neu-box/worker/neu_box.db` | SQLite 数据库 |
| `NEU_BOX_TASK_LOG_DIR` | `/var/lib/neu-box/worker/task-logs` | 任务日志目录 |
| `NEU_BOX_LOG_DIR` | `/var/log/neu-box` | 服务日志目录 |
| `NEU_BOX_BACKUP_DIR` | `/var/backups/neu-box` | 数据库备份目录 |
| `NEU_BOX_SANDBOX_EXECUTABLE` | `/usr/libexec/neu-box/neu-box-sandbox` | 沙盒管理 CLI |
| `NEU_BOX_DEVICE_INFO_SCRIPT` | `/usr/share/neu-box/info/npu_info.sh` | 设备状态脚本；GPU 节点改为 `gpu_info.sh` |
| `NEU_BOX_SANDBOX_REAPER_INTERVAL` | `30` | Reaper 扫描间隔（秒） |
| `NEU_BOX_COMMAND_TIMEOUT` | `0` | 命令超时（秒），`0` 表示不限制 |
| `NEU_BOX_COMMAND_MAX_COMPLETED` | `200` | 已完成任务保留上限 |
| `NEU_BOX_COMMAND_QUEUE_RECENT` | `200` | 状态接口返回的近期任务上限 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

新 Worker 只读取 `NEU_BOX_SANDBOX_EXECUTABLE`，不提供旧 shell 配置键或旧 BPF pin
ABI 的运行时兼容。Ascend 节点从 `/proc/devices` 动态读取 `devdrv-cdev` major；
接口缺失、任务设备号不匹配，或驱动重载后仍存在预留时，后续沙盒操作会拒绝继续。
驱动卸载或重载前必须先排空任务和 sandbox 并执行 cleanup，不能把 major 热切换
当作受支持的运行时操作。

### 设备分配与外部占用

空闲设备 = 受管设备 − Worker 已分配设备 − 沙盒外占用。设备状态脚本在每次分配前
同步执行；查询失败、超时或返回 `total=0` 时，Worker 沿用上一次成功结果，避免把
外部占用误判为空闲。共享节点仍建议所有使用方统一通过 Neu Box 申请设备。

### Reaper 与设备回收

Reaper 以内核 `cgroup.events` 的递归 `populated` 状态和整个子层级的
`cgroup.procs` 为准；数据库 PID 只是快照。空 cgroup 经过保护期和两次确认后才会
销毁，并同步清理 eBPF 设备预留。native 清理失败时会保留数据库记录，等待下个周期
重试；native 状态与 SQLite 不是单一事务，因此 native 清理成功后若数据库删除失败，
可能留下可重试的陈旧数据库记录，但不会恢复已经释放的 BPF 设备预留。

## 运行目录

```text
/etc/neu-box/worker.env                 Worker 配置
/var/lib/neu-box/worker/neu_box.db      Worker SQLite 数据库
/var/lib/neu-box/worker/task-logs/      任务日志（部署流程不迁移、不删除）
/var/log/neu-box/                       Worker 日志
/var/backups/neu-box/                   显式创建的数据库备份
/run/neu-box/sandbox-state/             沙盒运行时状态
```

## API 与实机验收

`/healthz` 与 `/status` 上报 `api_version`（当前 `2`）。部署后可运行：

```bash
./run.sh deployment-test
```

跨 Master/Worker 的集成测试见 [tests/README.md](../tests/README.md)。
