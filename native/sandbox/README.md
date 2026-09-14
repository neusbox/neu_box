# Worker sandbox resources

Neu Box Worker 使用 C++17 `neu-box-sandbox` 管理 cgroup v2 和 eBPF 设备隔离。

构建需要 GNU Make、C++17 编译器、支持 BPF target 的 Clang、
`pkg-config` 和 libbpf 1.0+ 开发包。

RPM 目标节点需要 root、cgroup v2、bpffs、systemd 以及 RPM 自动解析出的
libbpf 和其运行依赖；不需要 `sandbox.sh`、`bpftool`、`busctl`、clang、
Make 或 Python。

源码、构建、CLI、运行状态和设备隔离语义见
[沙盒说明](../../docs/sandbox.md)。

## 格式化与静态检查

`native/sandbox/.clang-format` 以 LLVM 风格为基础，保留项目使用的 4 空格缩进、
左侧指针、同一行大括号和 100 列宽。`native/sandbox/.clang-tidy` 开启
`modernize-use-trailing-return-type`，检查并可将函数转换为
`auto function(...) -> ReturnType` 形式。

```bash
make -C native/sandbox format
make -C native/sandbox lint
```

`clang-format` 和 `clang-tidy` 是开发检查工具，不会被打进 RPM。
