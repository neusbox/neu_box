# Worker sandbox resources

Neu Box Worker 默认使用 `v2/sandbox.sh` 管理 cgroup v2 和 eBPF 设备隔离。

运行依赖：

- root；
- cgroup v2；
- Bash、`bpftool`、`busctl`；
- 发布包构建阶段预编译并随版本分发的 `device_block.o`。

目标节点不需要 clang、libbpf 开发包或 Python。`device_block.bpf.c` 只作为源码保留；`deploy/build_release.py` 在构建发布包时使用 clang 生成 `device_block.o`，运行阶段只加载该预编译对象。
