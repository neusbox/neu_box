# Worker sandbox resources

Neu Box Worker 使用 C++17 `neu-box-sandbox` 管理 cgroup v2 和 eBPF 设备隔离。

构建需要 CMake、C++17 编译器、支持 BPF target 的 Clang、libbpf 静态库，
以及 libelf/zstd/zlib 开发库。

RPM 目标节点需要 root、cgroup v2、bpffs、systemd 以及 RPM 自动解析出的
libelf/zstd/zlib 运行库；不需要 `sandbox.sh`、`bpftool`、`busctl`、clang、
libbpf 动态库或 Python。

源码、构建、CLI、运行状态和设备隔离语义见
[沙盒说明](../../docs/sandbox.md)。
