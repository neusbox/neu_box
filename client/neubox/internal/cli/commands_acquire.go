package cli

import (
	"errors"
	"fmt"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"time"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

type terminalAcquireRequest struct {
	Username  string   `json:"username"`
	PID       int      `json:"pid"`
	DeviceNum int      `json:"device_num"`
	DeviceIDs []string `json:"device_ids"`
	CPU       int      `json:"cpu"`
	Memory    int      `json:"memory"`
	MemUnit   string   `json:"mem_unit"`
}

type acquireResponse struct {
	SandboxName string   `json:"sandbox_name"`
	Devices     []string `json:"devices"`
	Error       string   `json:"error"`
}

func (a *app) runAcquire(args []string) int {
	options, err := parseAcquireOptions(args)
	if err != nil {
		return a.usageError(err.Error())
	}
	return a.runTerminalAcquire(options)
}

func parseAcquireOptions(args []string) (acquireOptions, error) {
	options := acquireOptions{}
	positionals := make([]string, 0, 3)

	for index := 0; index < len(args); index++ {
		argument := args[index]
		if handled, err := consumeResourceOption(args, &index, &options.resourceOptions); handled || err != nil {
			if err != nil {
				return options, err
			}
			continue
		}

		switch argument {
		case "--pid":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.pid, err = positiveInteger("--pid", raw)
			if err != nil {
				return options, err
			}
			options.pidSet = true
		case "--container":
			return options, errors.New("acquire 仅支持宿主 PID；请在宿主 shell 申请沙盒后启动容器")
		case "--command", "--workdir", "--container-user", "--env", "--":
			return options, errors.New("命令任务已移至 submit；请使用 neubox submit [选项] -- <command>")
		default:
			if strings.HasPrefix(argument, "-") {
				return options, fmt.Errorf("未知 acquire 选项: %s", argument)
			}
			positionals = append(positionals, argument)
		}
	}

	if len(positionals) > 3 {
		return options, errors.New("acquire 最多接受 3 个位置参数: device_num cpu mem；命令任务请使用 submit")
	}
	if err := applyPositionalResources(&options.resourceOptions, positionals); err != nil {
		return options, err
	}
	if err := validateResourceOptions(&options.resourceOptions); err != nil {
		return options, err
	}
	return options, nil
}

func (a *app) runTerminalAcquire(options acquireOptions) int {
	shellPID := a.getPPID()
	if options.pidSet {
		shellPID = options.pid
	}
	if a.insideContainer() {
		return a.usageError("acquire 仅支持宿主 PID，不能从容器内申请；请在宿主 shell 申请沙盒")
	}
	// SIGINT 的 handler 必须**在第一次请求之前**就装好。
	//
	// Worker 一收到 acquire 请求就把它排进队列（`neubox acquire` 刚发出请求、
	// 还没开始轮询的那一瞬间，队列里就已经能看到 queued 的会话了）。如果这时
	// 用户按 Ctrl-C，而 handler 是轮询开始时才装的，Go 会走默认动作直接杀掉
	// 进程：退出码 -2、不取消、队列里留一条没人管的请求（真机用例 66 抓到的
	// 就是这个窗口）。所以这里先装好，并在拿到 acquire_id 之后立刻补一次取消。
	interrupts := make(chan os.Signal, 1)
	signal.Notify(interrupts, os.Interrupt)
	defer signal.Stop(interrupts)
	interruptPending := func() bool {
		select {
		case <-interrupts:
			return true
		default:
			return false
		}
	}
	interrupted := interruptPending()
	payload := terminalAcquireRequest{
		Username:  a.config.username,
		PID:       shellPID,
		DeviceNum: options.deviceNum,
		DeviceIDs: options.deviceIDs,
		CPU:       options.cpu,
		Memory:    options.memory,
		MemUnit:   "GB",
	}

	status, raw, err := a.worker.Request(http.MethodPost, "/sandbox/acquire", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	if status == http.StatusAccepted {
		var queued struct {
			AcquireID string `json:"acquire_id"`
		}
		if err := api.DecodeJSON(raw, &queued); err != nil || queued.AcquireID == "" {
			return a.internalError("invalid_worker_response", errors.New("Worker 排队响应缺少 acquire_id"))
		}
		if interrupted || interruptPending() {
			// Ctrl-C 在请求飞行途中就按下去了：请求已经排上队，直接取消。
			return a.cancelQueuedAcquire(queued.AcquireID)
		}
		// 阻塞在这里轮询，直到拿到卡；期间 Ctrl-C 会走取消路径。
		for status == http.StatusAccepted {
			select {
			case <-interrupts:
				return a.cancelQueuedAcquire(queued.AcquireID)
			case <-time.After(acquirePollInterval()):
			}
			status, raw, err = a.worker.Request(http.MethodGet, "/sandbox/acquire/"+url.PathEscape(queued.AcquireID), nil, nil)
			if err != nil {
				return a.requestError(err)
			}
			if err := api.ResponseError(status, raw); err != nil {
				return a.workerFailure(status, raw)
			}
		}
	}
	var response acquireResponse
	if err := api.DecodeJSON(raw, &response); err != nil || response.SandboxName == "" {
		if err == nil {
			err = errors.New("Worker 响应缺少 sandbox_name")
		}
		return a.internalError("invalid_worker_response", err)
	}
	if interrupted || interruptPending() {
		// 卡已经拿到、但用户在请求飞行途中就按了 Ctrl-C：意图是"不要了"，
		// 所以把刚建的沙盒释放掉再按 130 退出 —— 不能把一张卡留在场上。
		fmt.Fprintln(a.errOut, "[neubox] 收到 Ctrl-C，释放刚创建的沙盒")
		status, raw, err := a.worker.Request(http.MethodPost, "/sandbox/release", nil, map[string]any{
			"sandbox_name": response.SandboxName,
			"host_pid":     a.getPID(),
		})
		if err != nil {
			fmt.Fprintf(a.errOut, "释放沙盒 %s 失败: %v（请手动 neubox release %s）\n",
				response.SandboxName, err, response.SandboxName)
		} else if apiErr := api.ResponseError(status, raw); apiErr != nil {
			fmt.Fprintf(a.errOut, "释放沙盒 %s 失败: HTTP %d %s（请手动 neubox release %s）\n",
				response.SandboxName, status, strings.TrimSpace(string(raw)),
				response.SandboxName)
		} else {
			fmt.Fprintf(a.out, "[neubox] 沙盒已释放: %s\n", response.SandboxName)
		}
		return 130
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	devices := "无"
	if len(response.Devices) > 0 {
		devices = strings.Join(response.Devices, ",")
	}
	fmt.Fprintln(a.out, "[neubox] 沙盒已创建")
	fmt.Fprintf(a.out, "    sandbox: %s\n", response.SandboxName)
	fmt.Fprintf(a.out, "    pid: %d\n", shellPID)
	fmt.Fprintf(a.out, "    devices: %s\n", devices)
	fmt.Fprintf(a.out, "    release: neubox release %s\n", response.SandboxName)
	fmt.Fprintln(a.out, "    docker: neubox docker run -- <docker run 参数>")
	return 0
}

// acquirePollInterval 是排队期间的轮询间隔：0.5~1s 带抖动。
//
// 以前固定 100ms —— 排队几分钟就是每秒 10 次请求，纯属白打；抖动是为了避免多个
// 客户端整齐划一地同时打上来。
func acquirePollInterval() time.Duration {
	const base = 500 * time.Millisecond
	return base + time.Duration(rand.Int63n(int64(base)))
}

// cancelQueuedAcquire 处理"排队期间按了 Ctrl-C"：发一次统一取消请求就结束。
//
// 一次调用一个结果：还在排队就摘出队列，已经拿到卡就由 Worker 在同一个请求里做
// 释放（不需要客户端再补一次 release）。无论哪种结果，都按 Ctrl-C 的约定退出
// （130），因为用户的意图就是"不要了"。
func (a *app) cancelQueuedAcquire(acquireID string) int {
	status, raw, err := a.worker.Request(
		http.MethodDelete,
		"/tasks/"+url.PathEscape(acquireID)+"?kind=acquire",
		nil,
		map[string]any{"host_pid": a.getPID()},
	)
	if err != nil {
		fmt.Fprintf(a.errOut, "取消排队中的 acquire 失败: %v\n", err)
		return 130
	}
	if err := api.ResponseError(status, raw); err != nil {
		fmt.Fprintf(a.errOut, "取消排队中的 acquire 失败: HTTP %d %s\n",
			status, strings.TrimSpace(string(raw)))
		return 130
	}
	var response struct {
		Status      string `json:"status"`
		SandboxName string `json:"sandbox_name"`
	}
	if err := api.DecodeJSON(raw, &response); err != nil {
		fmt.Fprintf(a.errOut, "取消排队中的 acquire 失败: 响应无法解析\n")
		return 130
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 130
	}
	if response.Status == "released" {
		fmt.Fprintf(a.out, "[neubox] 已经拿到卡，沙盒已释放: %s\n", response.SandboxName)
	} else {
		fmt.Fprintln(a.out, "[neubox] 已取消排队中的 acquire")
	}
	return 130
}
