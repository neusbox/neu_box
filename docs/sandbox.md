# Native sandbox

`neu-box-sandbox` 是 Neu Box Worker 的 C++17 特权 helper。它直接操作
cgroup v2 文件系统，并通过 libbpf API 加载、校验和维护
`BPF_PROG_TYPE_CGROUP_DEVICE` 程序及 maps。Worker 通过这个程序
完成 sandbox 的创建、进程加入、设备预留和回收；不再使用
`sandbox.sh` 或运行时 `bpftool`。

## 源码与产物

```
native/sandbox/
├── CMakeLists.txt
├── bpf/device_block.bpf.c
├── src/                         C++17 用户态实现
└── tests/reservation_test.cpp
```

CMake 一次构建会产生：

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

默认生产构建使用 C++17，并把 libbpf 静态链接进 native CLI：

```bash
cmake -S native/sandbox -B build/native-sandbox \
  -DCMAKE_BUILD_TYPE=Release \
  -DNEU_BOX_SANDBOX_STATIC_LIBBPF=ON
cmake --build build/native-sandbox --parallel
ctest --test-dir build/native-sandbox --output-on-failure
```

构建机需要 CMake 3.20+、C++17 编译器、clang BPF backend、`pkg-config`、
libbpf 1.0+ 头文件、libbpf 静态库，以及 libelf/zstd/zlib 开发库。
`NEU_BOX_SANDBOX_STATIC_LIBBPF=OFF` 只用于开发构建；这种产物依赖目标机
的 libbpf 共享库，RPM 封装时必须显式按 `.dev` 包处理。生产构建仍会动态
链接 libelf、zstd、zlib；RPM 会根据 ELF 依赖自动生成目标机 Requires。

## 目标节点要求

生产 RPM 的目标节点需要：

- Linux 5.7+ 且启用 cgroup v2 和 cgroup device BPF；
- 可写的 cgroup v2 root `/sys/fs/cgroup` 以及已挂载的 bpffs
  `/sys/fs/bpf`；
- root 或等价的 cgroup/BPF 权限；
- systemd（管理 Worker 系统服务）。

生产目标节点不需要 `busctl`、`bpftool`、libbpf 动态库、clang、
CMake 或系统 Python 环境。native CLI 直接管理 cgroup 目录，不通过
systemd D-Bus
创建或删除 sandbox cgroup。

`bpf_get_current_ancestor_cgroup_id(1)` 用于把 sandbox 下的容器或
systemd 子 cgroup 归一到顶层 sandbox owner，因此内核最低版本为
5.7。

## CLI

```text
neu-box-sandbox [--bpf-object PATH] COMMAND [ARGS...]
```

| 命令 | 作用 |
| --- | --- |
| `load` | 幂等地确保 BPF 程序、maps、pins 和 root-cgroup attachment 处于当前 ABI，并刷新 `devdrv-cdev` major。 |
| `create <name> <cpu> <mem> [device ...]` | 创建 `/sys/fs/cgroup/sandbox_<name>`，写入 CPU/内存限制并预留设备。`cpu=0` 和 `mem=0` 表示不限；内存单位支持 K/M/G。 |
| `join <name> <PID>` | 验证 BPF attachment 后，把已存在的进程写入 sandbox 的 `cgroup.procs`。 |
| `status <name>` | 输出 cgroup CPU/内存状态、全局设备预留 maps 和该 sandbox 进程。 |
| `destroy <name>` | 终止 sandbox 层级中的进程，删除 cgroup、该 owner 的 map 条目和恢复状态。 |
| `list` | 列出现存的 `sandbox_*` cgroup；已有 pins 时校验随包程序身份、pins、root attachment 和动态 major，无 pins 时确认 BPF object 存在且没有 cgroup/attachment 残留。 |
| `cleanup` | 销毁所有 `sandbox_*` cgroup，然后 detach/unpin BPF 程序和 maps，并删除恢复状态。 |

`device` 接受 `major:minor`、`major:*` 或 `major`，后两者都表示
major 通配。`destroy` 和 `cleanup` 会终止进程，
执行前应先确认任务已结束。

## 运行时布局

| 状态 | 路径 |
| --- | --- |
| sandbox cgroup | `/sys/fs/cgroup/sandbox_<name>` |
| BPF program pin | `/sys/fs/bpf/device_block` |
| BPF map pins | `/sys/fs/bpf/sandbox_maps/{reserved_devices,reserved_majors,devdrv_major}` |
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
覆盖父级程序的挂载方式。
`list`/`status` 也会确认 `devdrv_major` map 与 `/proc/devices` 一致。
部分缺失、同名外来程序、旧版 program、major 漂移，或 cgroup 存在但
pin 丢失时，CLI 会 fail closed，不自动 detach 或热替换未知程序。
内核公开的 program tag 为 8 字节，这里用于发现陈旧或错配产物；它不是抵御
恶意 root 的密码学证明，而 root 本身已经具备 detach/替换 BPF 的权限。

## 设备预留语义

Ascend `devdrv-cdev` major 是动态分配的。CLI 每次 ensure/load 时从
`/proc/devices` 解析它，并写入 `devdrv_major` map；源码不硬编码
`235` 或任何其他 major。驱动卸载或重载不是在线操作：必须先排空任务和
sandbox，并执行 cleanup。BPF 程序自身不能读取 `/proc/devices`；如果特权
运维在活跃 sandbox 期间异步更换 major，下一次 CLI 操作只能检测并拒绝继续，
不能安全迁移已经存在的预留。

BPF 只处理两类字符设备 major：动态的 `devdrv-cdev` major 和 NVIDIA
major 195；`195:255` 显式共享，其他字符设备和全部块设备放行。
`reserved_devices` 保存精确或通配预留的 owner，`reserved_majors`
限制已在该 major 上获得设备的 sandbox 访问其他 minor。

这是“全局设备预留”，不是每个 sandbox 的完整设备 allowlist：
未被任何 sandbox 预留的设备仍可由其他 cgroup 打开。设备 cgroup
检查发生在打开设备节点时，不会追溯撤销进程在加入 sandbox 之前
已经打开的 file descriptor。

更详细的内核调用链和 map 决策见
[`syscall-block-flow.md`](syscall-block-flow.md)。
