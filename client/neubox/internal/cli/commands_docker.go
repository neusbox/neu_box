package cli

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
	"github.com/neusbox/neu_box/client/neubox/internal/dockerargs"
)

const (
	dockerRunUsage   = "neubox docker run <docker run 参数...>"
	dockerStartUsage = "neubox docker start <容器> [docker start 的其他参数...]"

	// `docker start` 之后回查借条认领结果的节奏。hook 在 create 阶段就登记完
	// 了，正常第一次查就有结果；这两个数只是容忍抖动。
	dockerStartConfirmTimeout = 3 * time.Second
	dockerStartConfirmPoll    = 100 * time.Millisecond
)

// runDocker 分发 `neubox docker run` / `neubox docker start`。
//
// 客户端不碰 BPF、不碰 cgroup、不做授权判断 —— 它只负责把"这次启动该用哪个
// 沙盒"讲清楚：run 拼一行 annotation，start 存一张借条（annotation 改不了）。
// 讲错了 Worker 会在登记时拒掉，容器在设备侧被 fail-closed 拦下。
func (a *app) runDocker(args []string) int {
	if len(args) == 0 {
		return a.usageError("用法: " + dockerRunUsage)
	}
	switch args[0] {
	case "run":
	case "start":
		return a.runDockerStart(args[1:])
	case "help", "-h", "--help":
		a.printDockerHelp()
		return 0
	default:
		return a.usageError(fmt.Sprintf(
			"docker 目前只支持 run 和 start，不支持 %s", args[0]))
	}

	// `docker run` 之后的参数一个都不解析，原样透传（连同开头的 --）。
	sandboxName, code := a.resolveOwnSandbox()
	if code != 0 {
		return code
	}

	dockerBinary, err := a.lookPath("docker")
	if err != nil {
		a.printError("docker_not_found",
			"PATH 里找不到 docker 命令；请在装了 docker 的主机上运行 neubox docker run")
		return 1
	}

	argv := dockerargs.BuildDockerArgs(sandboxName, args[1:])
	// execve 的 argv[0] 必须是可执行文件名本身，syscall.Exec 不会替你补。
	// 少了它，docker 进程看到的 os.Args 就是 ["run", "--annotation", ...]，
	// 于是 --annotation 落到顶层 flag 的位置，报 "unknown flag: --annotation"。
	full := append([]string{dockerBinary}, argv...)
	// 成功即进程已被替换，不会再走到这里。
	if err := a.execFn(dockerBinary, full, os.Environ()); err != nil {
		a.printError("docker_exec_failed", fmt.Sprintf("启动 docker 失败: %v", err))
		return 1
	}
	return 0
}

// runDockerStart 是 `neubox docker start`：先把这个 shell 的沙盒借给容器，
// 再真去 start。
//
// 为什么需要这一层：容器上那行 annotation 是**建容器时**写死的，而
// `docker start` 既不接受 `--annotation`、也看不到调用方是谁（它是 dockerd
// 干的活）。沙盒一旦 release，老容器再 start 就只剩"起得来但零卡"。这里把
// "这次 start 该用哪个沙盒"显式告诉 Worker（借条，10 秒内有效、一次性），
// hook 登记时认领；不经过 neubox 的 start 没有借条，照旧零卡。
func (a *app) runDockerStart(args []string) int {
	if len(args) == 0 || strings.HasPrefix(args[0], "-") {
		return a.usageError("用法: " + dockerStartUsage +
			"（容器名放最前面，docker 自己的选项跟在它后面）")
	}
	if a.insideContainer() {
		return a.usageError("容器内不能按 PID 反查沙盒（PID namespace 与宿主机" +
			"不同）；请在宿主 shell 里执行 neubox docker start")
	}
	container := args[0]

	dockerBinary, err := a.lookPath("docker")
	if err != nil {
		a.printError("docker_not_found",
			"PATH 里找不到 docker 命令；请在装了 docker 的主机上运行 neubox docker start")
		return 1
	}
	// 借条是加分项，不是门禁：借不上照样 start，容器只是拿不到卡（跟原生
	// docker start 一个结果）。卡有没有借到我们明说，不用退出码代替。
	containerID, sandboxName := a.lendSandbox(dockerBinary, container)

	// 借条已经落账。用子进程而不是 execve：start 之后还要回查认领结果。
	argv := append([]string{dockerBinary, "start"}, args...)
	status, err := a.runFn(dockerBinary, argv, os.Environ())
	if err != nil {
		a.printError("docker_exec_failed", fmt.Sprintf("启动 docker 失败: %v", err))
		return 1
	}
	if status != 0 || sandboxName == "" {
		return status
	}
	return a.confirmStartBinding(container, containerID, sandboxName)
}

// lendSandbox 尽力借沙盒：借到了返回 ``(容器 ID, 沙盒名)``，借不到返回空串并
// 打一行警告。容器找不到、不在沙盒里、沙盒正在销毁，都只是"没借到"。
func (a *app) lendSandbox(dockerBinary, container string) (string, string) {
	containerID, err := a.inspectContainerID(dockerBinary, container)
	if err != nil {
		a.printWarning("docker_inspect_failed", fmt.Sprintf(
			"查不到容器 %s 的 ID（%v），这次不借沙盒：容器起来了也没有 NPU",
			container, err))
		return "", ""
	}
	sandboxName, err := a.lendSandboxTo(containerID)
	if err != nil {
		a.printWarning("sandbox_not_lent", fmt.Sprintf(
			"没借到沙盒（%v）：容器照常启动，但里面看不到 NPU", err))
		return containerID, ""
	}
	return containerID, sandboxName
}

// inspectContainerID 把容器名/短 ID 解析成完整容器 ID。
//
// 借条按 ID 记，hook 报上来的也是 ID：容器名可以被改，ID 不会。
func (a *app) inspectContainerID(dockerBinary, container string) (string, error) {
	raw, err := a.outputFn(dockerBinary, "inspect", "--format", "{{.Id}}", container)
	if err != nil {
		return "", fmt.Errorf("docker inspect 失败: %w", err)
	}
	containerID := strings.TrimSpace(string(raw))
	if len(containerID) != 64 {
		return "", fmt.Errorf("docker inspect 给出的容器 ID 不是 64 位十六进制：%q",
			containerID)
	}
	return containerID, nil
}

// lendSandboxTo 存借条：把本进程（= 当前 shell）所在的沙盒借给这个容器。
//
// Worker 侧自己按 PID 反查沙盒并校验属主，客户端报的就是自己的 PID —— 它说
// 不出一个不属于自己的沙盒名。查不到沙盒、沙盒不是自己的、沙盒正在销毁，都
// 在这一步被拒，容器不会被启动。
func (a *app) lendSandboxTo(containerID string) (string, error) {
	payload := map[string]any{
		"username":     a.config.username,
		"pid":          a.getPID(),
		"container_id": containerID,
	}
	status, raw, err := a.worker.Request(
		http.MethodPost, "/container/intent", nil, payload)
	if err != nil {
		return "", fmt.Errorf("请求 Worker 失败: %w", err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		message, code := api.ErrorDetails(status, raw)
		if code != "" {
			return "", fmt.Errorf("HTTP %d %s (%s)", status, message, code)
		}
		return "", fmt.Errorf("HTTP %d %s", status, message)
	}
	var response struct {
		SandboxName string `json:"sandbox_name"`
	}
	if err := api.DecodeJSON(raw, &response); err != nil {
		return "", fmt.Errorf("Worker 返回的不是合法 JSON: %w", err)
	}
	sandboxName := strings.TrimSpace(response.SandboxName)
	if sandboxName == "" {
		return "", errors.New("Worker 没有返回借出去的沙盒名")
	}
	return sandboxName, nil
}

// confirmStartBinding 回查借条有没有被认领。
//
// hook 在 create 阶段就登记完了，正常第一次查就是 consumed。没认领就明说：
// 容器起来了，但里面看不到 NPU —— 否则用户会以为卡借出去了，到容器里才发现
// 没有。start 本身成功了，退出码照 docker 的来。
func (a *app) confirmStartBinding(container, containerID, sandboxName string) int {
	deadline := time.Now().Add(dockerStartConfirmTimeout)
	for {
		state, err := a.startIntentState(containerID)
		if err != nil {
			a.printWarning("sandbox_binding_unknown", fmt.Sprintf(
				"无法确认容器 %s 有没有绑上沙盒 %s（%v）；容器已经起来了",
				container, sandboxName, err))
			return 0
		}
		if state == "consumed" {
			return a.printStarted(container, containerID, sandboxName)
		}
		if time.Now().After(deadline) {
			break
		}
		time.Sleep(dockerStartConfirmPoll)
	}
	a.printWarning("sandbox_not_bound", fmt.Sprintf(
		"容器 %s 绑沙盒 %s 没有生效：它起来了，但里面看不到 NPU（借条可能已过期）。"+
			"要卡可以在沙盒里再跑一次 neubox docker start %s",
		container, sandboxName, container))
	return 0
}

// startIntentState 查借条的认领状态；没有借条时返回空串。
func (a *app) startIntentState(containerID string) (string, error) {
	query := url.Values{
		"container_id": []string{containerID},
		"username":     []string{a.config.username},
		"pid":          []string{strconv.Itoa(a.getPID())},
	}
	status, raw, err := a.worker.Request(
		http.MethodGet, "/container/intent", query, nil)
	if err != nil {
		return "", err
	}
	if err := api.ResponseError(status, raw); err != nil {
		message, _ := api.ErrorDetails(status, raw)
		return "", fmt.Errorf("HTTP %d %s", status, message)
	}
	var response struct {
		State *string `json:"state"`
	}
	if err := api.DecodeJSON(raw, &response); err != nil {
		return "", err
	}
	if response.State == nil {
		return "", nil
	}
	return *response.State, nil
}

func (a *app) printStarted(container, containerID, sandboxName string) int {
	if a.jsonOutput {
		_ = printJSONValue(a.out, map[string]any{
			"container":    container,
			"container_id": containerID,
			"sandbox":      sandboxName,
			"bound":        true,
		})
		return 0
	}
	fmt.Fprintln(a.out, "[neubox] 容器已启动并绑上沙盒")
	fmt.Fprintf(a.out, "    container: %s\n", shorthandContainerID(containerID))
	fmt.Fprintf(a.out, "    sandbox: %s\n", sandboxName)
	return 0
}

// shorthandContainerID 用 docker 自己的短 ID 口径（前 12 位）。
func shorthandContainerID(containerID string) string {
	if len(containerID) > 12 {
		return containerID[:12]
	}
	return containerID
}

// resolveOwnSandbox 反查本进程所在的沙盒，失败时自己打印错误并返回非 0 退出码。
//
// neubox 是 acquire 过的那个 shell 的子进程，cgroup 成员身份是继承的，
// 所以报**自己的** PID 就能让 Worker 反查到沙盒名。查不到就报错让用户先
// acquire —— 不猜、也不退化成"不加 annotation 照样起"：没有 annotation 的
// 容器拿不到设备，而且是在容器内第一次初始化时才炸，比在命令行上报错难查得多。
func (a *app) resolveOwnSandbox() (string, int) {
	// 容器里 PID 是 namespace 内视角，报给宿主机 Worker 会撞上别的进程，
	// 反查出一个别人的沙盒名。这条是错误分支，不在主路径上；先报错，
	// 异常处理以后再说。
	if a.insideContainer() {
		a.printError("sandbox_lookup_unsupported",
			"容器内无法按 PID 反查沙盒（PID namespace 与宿主机不同）；"+
				"请改用原生 docker run --annotation sandbox_cgroup=<沙盒名>")
		return "", 1
	}

	selfPID := a.getPID()
	query := url.Values{"pid": []string{strconv.Itoa(selfPID)}}
	status, raw, err := a.worker.Request(http.MethodGet, "/sandbox/status", query, nil)
	if err != nil {
		return "", a.requestError(err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		return "", a.workerFailure(status, raw)
	}
	var response struct {
		SandboxName *string `json:"sandbox_name"`
	}
	if err := api.DecodeJSON(raw, &response); err != nil {
		return "", a.internalError("invalid_worker_response", err)
	}
	if response.SandboxName == nil || strings.TrimSpace(*response.SandboxName) == "" {
		a.printError("not_in_sandbox", fmt.Sprintf(
			"当前进程 (pid %d) 不在任何沙盒中；请先在 shell 里执行 neubox acquire，"+
				"或改用原生 docker run --annotation sandbox_cgroup=<沙盒名>", selfPID))
		return "", 1
	}
	return strings.TrimSpace(*response.SandboxName), 0
}

func (a *app) printDockerHelp() {
	fmt.Fprint(a.out, `neubox docker run   — 在沙盒里启动新容器
neubox docker start — 把已停的容器拉起来，并把当前沙盒借给它

用法:`+"\n    "+dockerRunUsage+`
    `+dockerStartUsage+`

说明:
    docker run 的参数一个不改，只在最前面补一行 annotation：
        neubox docker run --rm -it ubuntu bash
      = docker run --annotation sandbox_cgroup=<沙盒名> --rm -it ubuntu bash

    容器必须带这行 annotation：Worker 靠它把容器登记到沙盒名下，没登记的
    容器即使卡空着也一律拿不到设备（fail-closed）。runtime 不用写，
    neu-box-runtime 已经是默认 runtime。

    沙盒按本进程 PID 反查（neubox 是 acquire 过的 shell 的子进程，cgroup
    身份是继承的）；查不到直接报错，不会退化成不加 annotation 启动。

    docker start 的事多一层：容器上那行 annotation 是建容器时写死的，而
    `+"`docker start`"+` 既没有 --annotation、也看不到是谁在调。所以这里先把这个
    shell 的沙盒存成一张借条（10 秒内有效、一次性），hook 登记时认领；
    认领不到就是"容器起来了但零卡"。直接用原生 docker start 没有借条，
    同样零卡 —— 那条路不会被放行成"有卡"。

    没有自己的选项：要显式指定沙盒、或者要自己写 annotation，直接用原生
    docker run --annotation sandbox_cgroup=<沙盒名> ... 就好。
`)
}
