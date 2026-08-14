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
		fmt.Fprintln(a.out, version)
		return 0
	case "acquire", "a":
		return a.runAcquire(args[1:])
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
		fmt.Fprintf(a.errOut, "未知命令: %s\n\n", args[0])
		a.printHelpTo(a.errOut)
		return 2
	}
}

func (a *app) usageError(message string) int {
	fmt.Fprintln(a.errOut, message)
	return 2
}

func (a *app) requestError(err error) int {
	fmt.Fprintf(a.errOut, "请求 Worker 失败: %v\n", err)
	return 1
}

func (a *app) printHelp() {
	a.printHelpTo(a.out)
}

func (a *app) printHelpTo(writer io.Writer) {
	fmt.Fprint(writer, `neu-sbox — 终端沙盒隔离 / 命令任务提交

用法:
  neu-sbox acquire [选项...]
  neu-sbox release <sandbox_name> [--container NAME]
  neu-sbox {list|status|join|tasks|result} [参数]

acquire 选项:
  --devices 1,3       指定卡号，逗号分隔
  --device-num 2      自动分配卡数量；与 --devices 互斥
  --cpu 4             CPU 核数，0 表示不限
  --mem 8             内存 GB，0 表示不限
  --pid 12345         指定 PID；默认使用启动客户端的当前 shell
  --command "..."     提交一次性命令；不指定则管理当前终端
  --container NAME    容器终端身份；命令模式下表示执行目标
  --workdir PATH      已有容器命令的工作目录
  --container-user U  已有容器命令的用户
  --env KEY=VALUE     已有容器命令的环境变量，可重复

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
  neu-sbox acquire --devices 1 --command "npu-smi info"
  neu-sbox acquire --devices 1 --container training-01 --command "python train.py"
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
