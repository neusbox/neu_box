# Neu Box Worker 部署与升级手册

## 部署

```bash
# 首次安装：安装 RPM，确认配置，再执行 setup
sudo dnf install ./neuboxd-<version>-<release>.<arch>.rpm
sudoedit /etc/neu-box/worker.env          # 手动配置监听地址、设备过滤器、设备状态脚本等
sudo neuboxctl setup              # 检查/迁移数据库，启动 Worker，健康检查通过后恢复调度
curl -fsS http://127.0.0.1:59075/healthz
sudo neuboxctl test               # 部署后真机测试

# 升级：先 pause，再安装新包、setup、验收
sudo neuboxctl pause
sudo dnf install ./neuboxd-<version>-<release>.<arch>.rpm
sudo neuboxctl setup
sudo neuboxctl test
```

`pause` 是升级前必须执行的停服路径：排空任务和沙盒、备份数据库和配置、清理旧 BPF pins 后停止 Worker；`setup` 是唯一启动路径：迁移旧配置和 SQLite schema，加载新版 BPF，`/healthz` 通过后恢复调度。启动和停止统一走 `neuboxctl setup` / `pause`，只读观察使用 `systemctl status`、`journalctl`；等待超时可用 `--timeout <秒>` 调整。

部署后验收必须在维护窗口以 root 执行，会使用真实 API、任务、设备和容器。

## 部署依赖

目标节点机器要求：

| 项目 | 要求 | 必选/可选 |
|---|---|---|
| 操作系统 | Linux | 必选 |
| 架构 | `x86_64` / `aarch64` | 必选 |
| init | systemd | 必选 |
| cgroup | cgroup v2 | 必选 |
| 内核 | 5.11+，启用 cgroup device BPF | 必选 |
| BTF | `/sys/kernel/btf/vmlinux` 存在（`CONFIG_DEBUG_INFO_BTF=y`） | 必选 |
| 权限 | root 或可用的 sudo | 必选 |
| 必需工具 | `/bin/bash` | 必选 |
| 设备工具 | 对应厂商的驱动和状态工具，例如 `npu-smi` 或 `nvidia-smi` | 必选 |
| 容器场景 | Docker、Neu Box OCI runtime/hook（`neu_box_runtime`、`neu-box-hook`） | 可选 |

检查系统依赖：

```bash
uname -m
systemctl --version
test -f /sys/fs/cgroup/cgroup.controllers   # cgroup v2
test -f /sys/kernel/btf/vmlinux             # BPF CO-RE 必需
ldconfig -p | grep libbpf                   # native sandbox 动态链接依赖，由 dnf 安装

# 容器场景（可选）
command -v docker
command -v neu-box-hook
docker info --format '{{.DefaultRuntime}}'   # 期望 neu-box-runtime
```

`/sys/kernel/btf/vmlinux` 缺失时 BPF CO-RE 无法加载，Worker `setup` 健康检查会失败。

## 构建打包

构建机需要 Linux，架构通常与目标机一致。依赖如下：

| 依赖 | 要求 | 用途 |
|---|---|---|
| `uv` | - | 管理 Python 依赖和构建环境 |
| PyInstaller | - | 打包 Worker 与验收套件 |
| GNU Make | - | 构建 native sandbox |
| C++17 编译器 | - | 编译 native sandbox |
| Clang | 支持 BPF target | 编译 `device_block.o` |
| `pkg-config` | - | 发现并链接 libbpf |
| libbpf | 1.0+ 开发包 | native sandbox 链接 |
| `rpmbuild` | - | 生成 RPM |
| binutils | 提供 `readelf` | 解析 ELF 依赖 |

检查构建依赖：

```bash
uname -m
uv --version
uv run --frozen --group build pyinstaller --version
command -v make
command -v c++
command -v clang
command -v pkg-config
pkg-config --modversion libbpf
command -v rpmbuild
command -v readelf
```

构建前如果 shell 里已经激活了别的虚拟环境，先清掉：

```bash
unset VIRTUAL_ENV
uv sync --frozen --all-groups
```

构建命令：

```bash
uv run --frozen --group build deploy/build_release.py
```

开发后、构建发布前需确保测试通过，详见[测试文档](testing.md)。

构建完成后检查产物：

```bash
ls -l dist/rpm/neuboxd-*.rpm

rpm -qpi dist/rpm/neuboxd-*.rpm
rpm -qpl dist/rpm/neuboxd-*.rpm | grep -E 'neuboxctl|neu-box-deployment-tests'
rpm -qpR dist/rpm/neuboxd-*.rpm
```

构建产物本地自检：

```bash
build/release/pyinstaller-dist/neu-box-deployment-tests/neu-box-deployment-tests --self-check
```

## 程序的运行时组织

升级时按类别处理：程序文件由 RPM 管理，配置和数据库由 `setup` 迁移，BPF 和运行时状态由 `pause` 清理、`setup` 重建。路径如下：

```shell
# 程序文件：RPM 安装和管理，不迁移
/usr/libexec/neu-box/neuboxd/                   neuboxd daemon bundle
/usr/libexec/neu-box/neuboxctl/                 neuboxctl 管理 CLI bundle
/usr/libexec/neu-box/device_block.o             预编译 BPF object
/usr/libexec/neu-box/neu-box-sandbox            native sandbox
/usr/sbin/neuboxd                                neuboxd 入口符号链接
/usr/sbin/neuboxctl                              neuboxctl 入口符号链接
/usr/lib/systemd/system/neuboxd.service         systemd unit

# 配置：setup 迁移旧键，不覆盖自定义值
/etc/neu-box/worker.env                         Worker 配置

# 数据库：setup 迁移 schema，pause 备份，pending 任务保留
/var/lib/neu-box/worker/neu_box.db              SQLite 数据库
/var/lib/neu-box/worker/neu_box.db.paused       暂停标记

# 数据与日志：不迁移、不删除；pause 备份数据库和配置
/var/lib/neu-box/worker/task-logs/              任务日志
/var/log/neu-box/                               Worker 日志
/var/backups/neu-box/                           数据库和配置备份

# 运行时状态：pause 清理，setup 重建
/run/neu-box/sandbox-state/                     沙盒运行时状态
```

`neuboxd` 只暴露 `serve` 守护进程入口；`setup`、`pause`、`resume`、`db`、`sandbox`、`test` 都由独立的 `neuboxctl` 提供。`neuboxctl` 的实现源码在 `src/neu_box/ctl.py`，由 PyInstaller 打成独立 onedir；配置变量见 [配置文档](configuration.md)。
