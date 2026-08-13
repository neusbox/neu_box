# Worker sandbox resources

Neu Box Worker 默认使用 `v2/sandbox.sh` 管理 cgroup v2 和 eBPF 设备隔离。

运行依赖：

- root；
- cgroup v2；
- Bash、`bpftool`、`busctl`；
- 发布包内与源码一起构建的 `device_block.o`。

目标节点不需要 clang、libbpf 开发包或 Python。`sandbox.sh compile` 仅供开发者手动重新编译 BPF 对象；正式发布由 `deploy/build_release.py` 在构建阶段完成编译。
