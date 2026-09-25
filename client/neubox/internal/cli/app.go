package cli

import (
	"fmt"
	"io"
	"os"
	"os/user"
	"strconv"
	"strings"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

var version = "dev"

const defaultWorkerURL = "http://127.0.0.1:59075"

type config struct {
	workerURL string
	username  string
}

type app struct {
	config          config
	out             io.Writer
	errOut          io.Writer
	jsonOutput      bool
	worker          *api.Client
	getPID          func() int
	getPPID         func() int
	insideContainer func() bool
	readFile        func(string) ([]byte, error)
	lookPath        LookPathFn
	execFn          ExecFn
	outputFn        OutputFn
	runFn           RunFn
}

// Run executes one CLI invocation and returns its process exit code.
func Run(args []string, out, errOut io.Writer) int {
	return newApp(out, errOut).run(args)
}

func newApp(out, errOut io.Writer) *app {
	cfg := configFromEnvironment()
	return &app{
		config:          cfg,
		out:             out,
		errOut:          errOut,
		worker:          api.NewClient(cfg.workerURL),
		getPID:          os.Getpid,
		getPPID:         os.Getppid,
		insideContainer: runningInsideContainer,
		readFile:        os.ReadFile,
		lookPath:        defaultLookPath,
		execFn:          defaultExec,
		outputFn:        defaultOutput,
		runFn:           defaultRun,
	}
}

func configFromEnvironment() config {
	uid := os.Getuid()
	workerURL := strings.TrimRight(strings.TrimSpace(os.Getenv("NEU_BOX_URL")), "/")
	if workerURL == "" {
		workerURL = defaultWorkerURL
	}
	return config{
		workerURL: workerURL,
		username:  currentUsername(uid),
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
	case "check", "ck":
		return a.runCheck(args[1:])
	case "docker", "dk":
		return a.runDocker(args[1:])
	case "join", "j":
		return a.runJoin(args[1:])
	case "cancel", "c":
		return a.runCancel(args[1:])
	case "tasks", "t":
		return a.runTasks(args[1:])
	case "result", "res", "log", "l":
		return a.runResult(args[1:])
	case "wait", "w":
		return a.runWait(args[1:])
	default:
		if a.jsonOutput {
			a.printError("unknown_command", "未知命令: "+args[0])
		} else {
			fmt.Fprintf(a.errOut, "[neubox] 参数错误: 未知命令: %s\n\n", args[0])
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
