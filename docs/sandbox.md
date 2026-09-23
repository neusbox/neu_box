# Native sandbox

`neu-box-sandbox` 是 Neu Box Worker 的 C++17 特权 helper。它直接操作
cgroup v2 文件系统，并通过 libbpf API 加载、校验和维护
`BPF_PROG_TYPE_CGROUP_DEVICE` 程序及 maps。Worker 通过这个程序
完成 sandbox 的创建、进程加入、设备预留和回收；不再使用
`sandbox.sh` 或运行时 `bpftool`。

## 源码与产物

```
native/sandbox/
├── Makefile
├── bpf/device_block.bpf.c
├── src/                         C++17 用户态实现
└── tests/reservation_test.cpp
```

Make 一次构建会产生：

- `neu-box-sandbox`：native CLI；
- `device_block.o`：由 clang 预编译的 BPF ELF object。

生产 RPM 中只安装运行产物：

```
/usr/libexec/neu-box/neu-box-sandbox
/usr/libexec/neu-box/device_block.o
```

CLI 默认从自己所在目录读取 `device_block.o`。只有开发或诊断时
才应在命令前使用 `--bpf-object PATH` 显式指定其他对象。

## 构建

默认构建使用 C++17，并动态链接构建系统提供的 libbpf：

```bash
make -C native/sandbox BUILD_DIR="$PWD/build/native-sandbox"
make -C native/sandbox BUILD_DIR="$PWD/build/native-sandbox" test
```

构建机需要 GNU Make、C++17 编译器、clang BPF backend、
`pkg-config` 和 libbpf 1.0+ 开发包。RPM 会根据 native CLI 的 ELF
依赖自动生成目标机 libbpf Requires。

## 目标节点要求

生产 RPM 的目标节点需要：

- Linux 5.11+ 且启用 cgroup v2 和 cgroup device BPF；
- 可写的 cgroup v2 root `/sys/fs/cgroup` 以及已挂载的 bpffs
  `/sys/fs/bpf`；
- root 或等价的 cgroup/BPF 权限；
- systemd（管理 Worker 系统服务）。

目标节点的 libbpf 共享库由 RPM 依赖安装。节点不需要
`busctl`、`bpftool`、clang、Make 或系统 Python 环境。native CLI 直接管理
cgroup 目录，不通过
systemd D-Bus
创建或删除 sandbox cgroup。

容器进程由 mount namespace 识别 —— 与宿主机不同即视为容器，再查
`container_owner` map 找委托方沙盒；宿主机进程用
`bpf_get_current_cgroup_id()` 精确匹配自己的沙盒 cgroup，不向上找祖先。
目标节点的内核最低版本为 5.11：BPF 侧用 `bpf_get_current_task_btf()`
读当前任务的 mount namespace，该 helper 自 5.11 起提供。

## CLI

```text
neu-box-sandbox [--bpf-object PATH] [--device-major MAJOR] COMMAND [ARGS...]
```

`--device-major` 是 Python 侧发现的受管设备 major，`load` 和 `create` 必须显式提供。

| 命令 | 作用 |
| --- | --- |
| `load` | 幂等地确保 BPF 程序、maps、pins 和 root-cgroup attachment 处于当前 ABI。 |
| `create <name> <cpu> <mem> [major:minor ...]` | 创建 `/sys/fs/cgroup/sandbox_<name>`，写入 CPU/内存限制并预留设备。`cpu=0` 和 `mem=0` 表示不限；内存单位支持 K/M/G。 |
| `join <name> <PID>` | 验证 BPF attachment 后，把已存在的进程写入 sandbox 的 `cgroup.procs`。 |
| `bind-container <name> <mnt-ns>` | 把容器的 mount namespace 登记到该 sandbox 的授权上（`container_owner` map）。`mnt-ns` 是 namespace inum，由调用方 `open` + `fstat` 得到，调用方同时持有那个 fd 当 pin。与宿主机共用 mount namespace 时拒绝 —— 那不是容器。 |
| `unbind-container <mnt-ns>` | 删除一条容器登记。参数是 mount namespace inum 而不是 PID：容器退出后 `/proc/<pid>` 已消失，只能按登记时记下的 inum 删。 |
| `status <name>` | 输出 cgroup CPU/内存状态、全局设备预留 maps 和该 sandbox 进程。 |
| `destroy <name>` | 终止 sandbox 层级中的进程，删除 cgroup、该 owner 的 map 条目和恢复状态。 |
| `list` | 列出现存的 `sandbox_*` cgroup；已有 pins 时校验随包程序身份、pins 和 root attachment，无 pins 时确认 BPF object 存在且没有 cgroup/attachment 残留。 |
| `cleanup` | 销毁所有 `sandbox_*` cgroup，然后 detach/unpin BPF 程序和 maps，并删除恢复状态。 |

`device` 只接受 `major:minor`，major 和 minor 都是十进制数。`destroy` 和
`cleanup` 会终止进程，
执行前应先确认任务已结束。

## 运行时布局

| 状态 | 路径 |
| --- | --- |
| sandbox cgroup | `/sys/fs/cgroup/sandbox_<name>` |
| BPF program pin | `/sys/fs/bpf/device_block` |
| BPF map pins | `/sys/fs/bpf/sandbox_maps/{reserved_devices,reserved_majors,container_owner}` |
| 跨进程操作锁 | `/run/neu-box/sandbox.lock` |
| owner 恢复状态 | `/run/neu-box/sandbox-state/cgroup_id_<name>` |

所有命令都在同一把 `flock` 下读取或修改 cgroup、BPF maps 和恢复状态，
避免健康检查与生命周期操作互相竞态。`create` 在写 map 前先持久化 cgroup ID；
`destroy` 会在删除 cgroup 前再持久化实际 ID。如果中途失败，
状态会保留供后续 `destroy` 按 owner 重试，避免设备预留静默泄漏。

CLI 只接受完整且 ABI 匹配的 pin 布局，并会校验 program、maps、
program-map 关系和 root-cgroup attachment；已有 pinned program 的内核
tag 还必须与随包 `device_block.o` 在当前节点实际加载所得 tag 一致。
root attachment mode 必须精确为 `BPF_F_ALLOW_MULTI`，拒绝允许子 cgroup
覆盖父级程序的挂载方式。部分缺失、同名外来程序、旧版 program，或 cgroup 存在但
pin 丢失时，CLI 会 fail closed，不自动 detach 或热替换未知程序。
内核公开的 program tag 为 8 字节，这里用于发现陈旧或错配产物；它不是抵御
恶意 root 的密码学证明，而 root 本身已经具备 detach/替换 BPF 的权限。

## 设备预留语义

Worker 根据 `NEU_BOX_DEVICE_FILTER` 匹配 `/dev` 下的字符设备，通过
`stat(2)` 获取实际 `major:minor` 后传给 CLI。native CLI 和 BPF 不按
设备厂商维护固定的受管 major 列表。

`reserved_devices` 保存实际设备号的精确预留 owner；
`reserved_majors` 限制已在某个 major 上获得设备的 sandbox 访问其他
minor。`195:255` 作为 NVIDIA control device 显式共享，全部块设备放行。
驱动重载导致设备号变化时，已有 map 条目不会自动迁移，应先排空任务和
sandbox 并执行 cleanup。

这是“全局设备预留”，不是每个 sandbox 的完整设备 allowlist：
未被任何 sandbox 预留的设备仍可由其他 cgroup 打开。设备 cgroup
检查发生在打开设备节点时，不会追溯撤销进程在加入 sandbox 之前
已经打开的 file descriptor。

容器走的是另一条分支（按 mount namespace 查委托方沙盒，空闲卡也拒绝），
以及驱动那张按 mnt ns 缓存的 UDA 表为什么是"第二道门"：见
[`isolation.md`](isolation.md)。
