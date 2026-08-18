package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
)

var version = "dev"

const defaultWorkerURL = "http://127.0.0.1:59075"

type config struct {
	workerURL string
	username  string
	container string
	stateDir  string
}

type app struct {
	config          config
	out             io.Writer
	errOut          io.Writer
	jsonOutput      bool
	api             *apiClient
	getPID          func() int
	getPPID         func() int
	insideContainer func() bool
	readFile        func(string) ([]byte, error)
}

func main() {
	os.Exit(newApp(os.Stdout, os.Stderr).run(os.Args[1:]))
}

func newApp(out, errOut io.Writer) *app {
	cfg := configFromEnvironment()
	return &app{
		config:          cfg,
		out:             out,
		errOut:          errOut,
		api:             newAPIClient(cfg.workerURL),
		getPID:          os.Getpid,
		getPPID:         os.Getppid,
		insideContainer: runningInsideContainer,
		readFile:        os.ReadFile,
	}
}

func configFromEnvironment() config {
	uid := os.Getuid()
	workerURL := strings.TrimRight(strings.TrimSpace(os.Getenv("NEU_BOX_URL")), "/")
	if workerURL == "" {
		workerURL = defaultWorkerURL
	}
	stateDir := strings.TrimSpace(os.Getenv("NEU_BOX_STATE_DIR"))
	if stateDir == "" {
		stateDir = filepath.Join(os.TempDir(), fmt.Sprintf("neu-box-%d", uid))
	}
	return config{
		workerURL: workerURL,
		username:  currentUsername(uid),
		container: strings.TrimSpace(os.Getenv("NEU_BOX_CONTAINER")),
		stateDir:  stateDir,
	}
}

func currentUsername(uid int) string {
	for _, name := range []string{"NEU_BOX_USER", "USER", "LOGNAME"} {
		if value := strings.TrimSpace(os.Getenv(name)); value != "" {
			return value
		}
	}
	if current, err := user.Current(); err == nil {
		if value := strings.TrimSpace(current.Username); value != "" {
			return value
		}
	}
	return strconv.Itoa(uid)
}

func runningInsideContainer() bool {
	for _, path := range []string{"/.dockerenv", "/run/.containerenv"} {
		if _, err := os.Stat(path); err == nil {
			return true
		}
	}
	return false
}

func (a *app) run(args []string) int {
	args, a.jsonOutput = extractJSONOption(args)
	if len(args) == 0 {
		a.printHelp()
		return 0
	}
	if len(args) > 1 && (args[1] == "-h" || args[1] == "--help") {
		a.printHelp()
		return 0
	}

	switch args[0] {
	case "help", "-h", "--help":
		a.printHelp()
		return 0
	case "version", "-v", "--version":
		if a.jsonOutput {
			_ = printJSONValue(a.out, map[string]any{"version": version})
		} else {
			fmt.Fprintln(a.out, version)
		}
		return 0
	case "acquire", "a":
		return a.runAcquire(args[1:])
	case "submit", "sub":
		return a.runSubmit(args[1:])
	case "release", "r":
		return a.runRelease(args[1:])
	case "list", "ls":
		return a.runList(args[1:])
	case "status", "st":
		return a.runStatus(args[1:])
	case "join", "j":
		return a.runJoin(args[1:])
	case "tasks", "t":
		return a.runTasks(args[1:])
	case "result", "res", "log", "l":
		return a.runResult(args[1:])
	default:
		if a.jsonOutput {
			a.printError("unknown_command", "未知命令: "+args[0])
		} else {
			fmt.Fprintf(a.errOut, "[neu-sbox] 参数错误: 未知命令: %s\n\n", args[0])
			a.printHelpTo(a.errOut)
		}
		return 2
	}
}

func extractJSONOption(args []string) ([]string, bool) {
	if len(args) > 0 && args[0] == "--json" {
		return args[1:], true
	}
	if len(args) > 1 && args[1] == "--json" {
		filtered := append([]string{args[0]}, args[2:]...)
		return filtered, true
	}
	return args, false
}

func (a *app) usageError(message string) int {
	a.printError("usage_error", "参数错误: "+message)
	return 2
}

func (a *app) requestError(err error) int {
	a.printError("worker_request_failed", fmt.Sprintf("请求 Worker 失败: %v", err))
	return 1
}

func (a *app) internalError(code string, err error) int {
	a.printError(code, err.Error())
	return 1
}

func (a *app) printError(code, message string) {
	if a.jsonOutput {
		_ = printJSONValue(a.errOut, map[string]any{
			"code":  code,
			"error": message,
		})
		return
	}
	fmt.Fprintf(a.errOut, "[neu-sbox] %s\n", message)
}

func (a *app) printWarning(code, message string) {
	if a.jsonOutput {
		_ = printJSONValue(a.errOut, map[string]any{
			"code":    code,
			"warning": message,
		})
		return
	}
	fmt.Fprintf(a.errOut, "[neu-sbox] 警告: %s\n", message)
}

func (a *app) printHelp() {
	a.printHelpTo(a.out)
}

func (a *app) printHelpTo(writer io.Writer) {
	fmt.Fprint(writer, `neu-sbox — 终端沙盒隔离 / 命令任务提交

用法:
    neu-sbox [--json] acquire [选项...]
    neu-sbox [--json] submit [选项...] -- <command> [args...]
    neu-sbox [--json] release <sandbox_name> [--container NAME]
    neu-sbox [--json] {list|status|join|tasks|result} [参数]

全局选项:
    --json                 只输出机器可读 JSON；失败信息写入 stderr

输出说明:
    默认模式               输出终端可读的结果摘要，不混入 Worker 原始 JSON
    JSON 模式              --json 可放在子命令前或紧跟子命令
                           成功结果写入 stdout，失败对象写入 stderr
    result --json          在任务元数据中增加 log 字段
    submit ... -- ...      -- 后面的参数属于目标命令，不再解析为 neu-sbox 选项

命令说明:
    acquire                同步申请终端沙盒；返回成功时当前 shell 已进入沙盒
    submit                 异步提交命令任务；返回成功只表示任务已进入队列，
                           需使用 result <task_id> 查询结果和日志
    result <task_id>        查询异步任务的状态、执行结果和日志

acquire 选项:
    --device ID            指定一个卡号，可重复
    --devices 1,3          指定卡号，逗号分隔
    --device-num 2         自动分配卡数量；与 --device/--devices 互斥
    --cpu 4                CPU 核数，0 表示不限
    --mem 8                内存 GB，0 表示不限
    --pid 12345            指定 PID；默认使用启动客户端的当前 shell
    --container NAME       容器终端身份

submit 选项:
    --device ID            指定一个卡号，可重复
    --devices 1,3          指定卡号，逗号分隔
    --device-num 2         自动分配卡数量；与 --device/--devices 互斥
    --cpu 4                CPU 核数，0 表示不限
    --mem 8                内存 GB，0 表示不限
    --container NAME       在已有容器中执行；不指定则在 Host 执行
    --workdir PATH         已有容器命令的工作目录
    --container-user USER  已有容器命令的用户
    --env KEY=VALUE        已有容器命令的环境变量，可重复
    --command "..."        命令字符串；也可将命令放在 -- 后

其他命令:
    list                         列出沙盒和资源
    status                       查看当前 shell 所在 sandbox
    join <sandbox_name>          将 Host 当前 shell 加入已有 sandbox
    tasks                        查看任务队列
    result <task_id>             查看任务输出和结果
    version                      显示客户端版本

示例:
    neu-sbox acquire --device-num 1
    neu-sbox acquire --devices 1,3 --cpu 4 --mem 8
    neu-sbox acquire --container training-01 --device-num 1
    neu-sbox submit --device 1 -- npu-smi info
    neu-sbox submit --device 1 --container training-01 --workdir /workspace -- python train.py
    neu-sbox release sbx_yuxd_12345.slice

环境变量:
    NEU_BOX_URL            Worker 地址，默认 http://127.0.0.1:59075
    NEU_BOX_USER           sandbox/任务用户名
    NEU_BOX_CONTAINER      当前容器名称或 ID
    NEU_BOX_STATE_DIR      显式 --container 的 release 状态目录

容器终端不需要共享 Host PID namespace。客户端是静态二进制，运行时不依赖
Bash、curl 或 Python；目标容器仍须预先挂载可能申请的设备节点。
`)
}

func defaultHTTPClient() *http.Client {
	return &http.Client{
		Transport: &http.Transport{Proxy: nil},
		Timeout:   requestTimeout,
	}
}
