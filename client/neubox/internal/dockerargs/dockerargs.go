// Package dockerargs 把沙盒身份注入 docker run 的 argv。
//
// 这一层只做 argv 变换：不读环境变量、不访问 Worker、不 exec 进程，
// 所以能整个用纯函数单测覆盖。真正执行 docker 的是 internal/cli。
//
// 契约见 neu_box_worker/docs/runtime-hook.md「客户端」一节：
//
//	neubox docker run <docker run 参数...>
//	  → docker run --annotation sandbox_cgroup=<name> <原样透传>
package dockerargs

import "strings"

// AnnotationKey 是 Worker 侧认的 OCI annotation 键。
//
// 值必须是**沙盒名**（如 sbx_yuxd_12345.slice），不能是 cgroup 路径或
// basename：路径会被回收复用，沙盒销毁重建后旧 annotation 会指到一个
// 活的、别人的沙盒上。
const AnnotationKey = "sandbox_cgroup"

// BuildDockerArgs 返回 docker 的命令行参数（**不含 argv[0]**，即不含可执行
// 文件本身）：
//
//	run --annotation sandbox_cgroup=<annotation> <passthrough...>
//
// 调用方 exec 时必须自己把程序名放到最前面当 argv[0]：docker 解析的是
// os.Args[1:]，少了这一格，"run" 会被当成程序名吃掉，第一个参数变成
// --annotation，docker 把它当顶层 flag 拒掉（unknown flag）。
//
// **不注入 --runtime**：`neu-box-runtime` 已经是这台机器上的默认 runtime（daemon.json
// 的 default-runtime），用户不需要写，我们也不该替用户写 —— 写死了反而和
// 装机配置脱节。
//
// 注入放最前面，passthrough 一个字节都不动：客户端没有自己的选项，不解析、
// 不识别、不重排用户参数。要显式指定沙盒、或者要自己写 annotation 的，应该
// 绕开 neubox 直接用原生 docker（见契约「客户端没有自己的选项」），所以这里
// 也不会因为用户已经带了 sandbox_cgroup 就少注入一次。
//
// annotation 为空表示没有沙盒身份，此时不注入：这样起出来的容器没有登记，
// 会在设备侧被 BPF 拒掉（fail-closed）。调用方不该走到这一步 —— 定不出沙盒
// 时应当直接报错，而不是"不加 annotation 照样起"。
func BuildDockerArgs(annotation string, passthrough []string) []string {
	arguments := make([]string, 0, len(passthrough)+3)
	arguments = append(arguments, "run")

	if strings.TrimSpace(annotation) != "" {
		arguments = append(arguments, "--annotation", AnnotationKey+"="+annotation)
	}
	return append(arguments, passthrough...)
}
