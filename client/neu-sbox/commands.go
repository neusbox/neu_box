package main

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type acquireOptions struct {
	deviceIDs     []string
	deviceNum     int
	cpu           int
	memory        int
	priority      int
	pid           int
	pidSet        bool
	command       string
	container     string
	workdir       string
	containerUser string
	environment   map[string]string
}

type commandTarget struct {
	Type      string            `json:"type"`
	Container string            `json:"container"`
	Workdir   *string           `json:"workdir"`
	User      *string           `json:"user"`
	Env       map[string]string `json:"env"`
}

type commandRequest struct {
	UserID    string         `json:"user_id"`
	Command   string         `json:"command"`
	DeviceNum int            `json:"device_num"`
	DeviceIDs []string       `json:"device_ids"`
	CPU       int            `json:"cpu"`
	Memory    int            `json:"memory"`
	MemUnit   string         `json:"mem_unit"`
	Priority  int            `json:"priority"`
	Target    *commandTarget `json:"target,omitempty"`
}

type terminalAcquireRequest struct {
	Username  string   `json:"username"`
	PID       int      `json:"pid"`
	DeviceNum int      `json:"device_num"`
	DeviceIDs []string `json:"device_ids"`
	CPU       int      `json:"cpu"`
	Memory    int      `json:"memory"`
	MemUnit   string   `json:"mem_unit"`
	Container string   `json:"container,omitempty"`
}

type acquireResponse struct {
	SandboxName string   `json:"sandbox_name"`
	Devices     []string `json:"devices"`
	Error       string   `json:"error"`
}

type commandResponse struct {
	TaskID   string `json:"task_id"`
	Position int    `json:"position"`
	Priority int    `json:"priority"`
	Error    string `json:"error"`
}

func (a *app) runAcquire(args []string) int {
	options, err := parseAcquireOptions(args)
	if err != nil {
		return a.usageError(err.Error())
	}
	if options.command != "" {
		return a.runCommandAcquire(options)
	}
	return a.runTerminalAcquire(options)
}

func parseAcquireOptions(args []string) (acquireOptions, error) {
	options := acquireOptions{environment: make(map[string]string)}
	positionals := make([]string, 0, 4)

	for index := 0; index < len(args); index++ {
		argument := args[index]
		value := func() (string, error) {
			if index+1 >= len(args) {
				return "", fmt.Errorf("%s 缺少参数", argument)
			}
			index++
			return args[index], nil
		}

		switch argument {
		case "--devices":
			raw, err := value()
			if err != nil {
				return options, err
			}
			for _, item := range strings.Split(raw, ",") {
				if item = strings.TrimSpace(item); item != "" {
					options.deviceIDs = append(options.deviceIDs, item)
				}
			}
			if len(options.deviceIDs) == 0 {
				return options, errors.New("--devices 不能为空")
			}
		case "--device-num":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.deviceNum, err = nonNegativeInteger("--device-num", raw)
			if err != nil {
				return options, err
			}
		case "--cpu":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.cpu, err = nonNegativeInteger("--cpu", raw)
			if err != nil {
				return options, err
			}
		case "--mem":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.memory, err = nonNegativeInteger("--mem", raw)
			if err != nil {
				return options, err
			}
		case "--pid":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.pid, err = positiveInteger("--pid", raw)
			if err != nil {
				return options, err
			}
			options.pidSet = true
		case "--command":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.command = raw
		case "--container":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.container = strings.TrimSpace(raw)
			if options.container == "" {
				return options, errors.New("--container 不能为空")
			}
		case "--priority":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.priority, err = nonNegativeInteger("--priority", raw)
			if err != nil {
				return options, err
			}
		case "--workdir":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.workdir = raw
		case "--container-user":
			raw, err := value()
			if err != nil {
				return options, err
			}
			options.containerUser = raw
		case "--env":
			raw, err := value()
			if err != nil {
				return options, err
			}
			key, envValue, found := strings.Cut(raw, "=")
			if !found || strings.TrimSpace(key) == "" {
				return options, fmt.Errorf("--env 必须是 KEY=VALUE: %s", raw)
			}
			options.environment[key] = envValue
		default:
			if strings.HasPrefix(argument, "-") {
				return options, fmt.Errorf("未知 acquire 选项: %s", argument)
			}
			positionals = append(positionals, argument)
		}
	}

	if len(positionals) > 4 {
		return options, errors.New("acquire 最多接受 4 个位置参数: device_num cpu mem command")
	}
	var err error
	if len(positionals) > 0 && options.deviceNum == 0 && len(options.deviceIDs) == 0 {
		options.deviceNum, err = nonNegativeInteger("device_num", positionals[0])
		if err != nil {
			return options, err
		}
	}
	if len(positionals) > 1 && options.cpu == 0 {
		options.cpu, err = nonNegativeInteger("cpu", positionals[1])
		if err != nil {
			return options, err
		}
	}
	if len(positionals) > 2 && options.memory == 0 {
		options.memory, err = nonNegativeInteger("mem", positionals[2])
		if err != nil {
			return options, err
		}
	}
	if len(positionals) > 3 && options.command == "" {
		options.command = positionals[3]
	}
	if len(options.deviceIDs) > 0 {
		options.deviceNum = 0
	}
	if options.command == "" && (options.workdir != "" || options.containerUser != "" || len(options.environment) > 0) {
		return options, errors.New("--workdir/--container-user/--env 必须配合 --command")
	}
	if options.command != "" && options.container == "" && (options.workdir != "" || options.containerUser != "" || len(options.environment) > 0) {
		return options, errors.New("--workdir/--container-user/--env 必须配合 --container")
	}
	return options, nil
}

func nonNegativeInteger(name, raw string) (int, error) {
	value, err := strconv.Atoi(raw)
	if err != nil || value < 0 {
		return 0, fmt.Errorf("%s 必须是非负整数: %q", name, raw)
	}
	return value, nil
}

func positiveInteger(name, raw string) (int, error) {
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s 必须是正整数: %q", name, raw)
	}
	return value, nil
}

func nullableString(value string) *string {
	if value == "" {
		return nil
	}
	copy := value
	return &copy
}

func (a *app) runCommandAcquire(options acquireOptions) int {
	payload := commandRequest{
		UserID:    a.config.username,
		Command:   options.command,
		DeviceNum: options.deviceNum,
		DeviceIDs: options.deviceIDs,
		CPU:       options.cpu,
		Memory:    options.memory,
		MemUnit:   "GB",
		Priority:  options.priority,
	}
	if options.container != "" {
		payload.Target = &commandTarget{
			Type:      "docker_existing",
			Container: options.container,
			Workdir:   nullableString(options.workdir),
			User:      nullableString(options.containerUser),
			Env:       options.environment,
		}
	}

	fmt.Fprintf(a.out, "[neu-sbox] 提交任务: device=%d cpu=%d mem=%dG\n", options.deviceNum, options.cpu, options.memory)
	if options.priority > 0 {
		fmt.Fprintf(a.out, "[neu-sbox] 优先级: %d（数值越大越靠前）\n", options.priority)
	}
	if len(options.deviceIDs) > 0 {
		fmt.Fprintf(a.out, "[neu-sbox] 指定设备: %s\n", strings.Join(options.deviceIDs, ","))
	}
	fmt.Fprintf(a.out, "[neu-sbox] 命令: %s\n", options.command)
	fmt.Fprintf(a.out, "[neu-sbox] user=%s\n", a.config.username)
	if options.container != "" {
		fmt.Fprintf(a.out, "[neu-sbox] 现有 Docker container=%s\n", options.container)
	}

	status, raw, err := a.api.request(http.MethodPost, "/command/run", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	_ = printJSON(a.out, raw)
	if err := responseError(status, raw); err != nil {
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	var response commandResponse
	if err := decodeJSON(raw, &response); err != nil || response.TaskID == "" {
		if err == nil {
			err = errors.New("Worker 响应缺少 task_id")
		}
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	positionNote := ""
	if response.Priority > 0 {
		positionNote = fmt.Sprintf("（priority=%d）", response.Priority)
	}
	fmt.Fprintf(a.out, "\n✓ 任务已提交，ID=%s 队列位置 #%d%s\n", response.TaskID, response.Position, positionNote)
	fmt.Fprintf(a.out, "  查看日志: neu-sbox result %s\n", response.TaskID)
	return 0
}

func (a *app) runTerminalAcquire(options acquireOptions) int {
	shellPID := a.getPPID()
	if options.pidSet {
		shellPID = options.pid
	}
	container := options.container
	if container == "" {
		container = a.config.container
	}
	if a.insideContainer() && container == "" {
		return a.usageError("容器内申请必须用 --container NAME 或设置 NEU_BOX_CONTAINER")
	}
	payload := terminalAcquireRequest{
		Username:  a.config.username,
		PID:       shellPID,
		DeviceNum: options.deviceNum,
		DeviceIDs: options.deviceIDs,
		CPU:       options.cpu,
		Memory:    options.memory,
		MemUnit:   "GB",
		Container: container,
	}

	fmt.Fprintf(a.out, "[neu-sbox] 申请沙盒: device=%d cpu=%d mem=%dG\n", options.deviceNum, options.cpu, options.memory)
	if len(options.deviceIDs) > 0 {
		fmt.Fprintf(a.out, "[neu-sbox] 指定设备: %s\n", strings.Join(options.deviceIDs, ","))
	}
	fmt.Fprintf(a.out, "[neu-sbox] PID=%d user=%s\n", shellPID, a.config.username)
	if container != "" {
		fmt.Fprintf(a.out, "[neu-sbox] 当前容器=%s\n", container)
	}

	status, raw, err := a.api.request(http.MethodPost, "/sandbox/acquire", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	_ = printJSON(a.out, raw)
	if err := responseError(status, raw); err != nil {
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	var response acquireResponse
	if err := decodeJSON(raw, &response); err != nil || response.SandboxName == "" {
		if err == nil {
			err = errors.New("Worker 响应缺少 sandbox_name")
		}
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	if container != "" {
		if err := a.rememberContainer(response.SandboxName, container); err != nil {
			fmt.Fprintf(a.errOut, "警告: 无法保存容器标识；release 时请传 --container %s: %v\n", container, err)
		}
	}
	fmt.Fprintf(a.out, "\n✓ 沙盒已创建，PID %d 独占设备。释放: neu-sbox release %s\n", shellPID, response.SandboxName)
	return 0
}

func (a *app) runRelease(args []string) int {
	if len(args) != 1 && len(args) != 3 {
		return a.usageError("用法: neu-sbox release <sandbox_name> [--container NAME]")
	}
	sandboxName := strings.TrimSpace(args[0])
	if sandboxName == "" {
		return a.usageError("sandbox_name 不能为空")
	}
	container := a.config.container
	if len(args) == 3 {
		if args[1] != "--container" || strings.TrimSpace(args[2]) == "" {
			return a.usageError("用法: neu-sbox release <sandbox_name> [--container NAME]")
		}
		container = strings.TrimSpace(args[2])
	}
	if container == "" {
		if saved, err := a.savedContainer(sandboxName); err == nil {
			container = saved
		} else if !errors.Is(err, os.ErrNotExist) {
			fmt.Fprintf(a.errOut, "警告: 无法读取保存的容器标识: %v\n", err)
		}
	}
	if a.insideContainer() && container == "" {
		fmt.Fprintln(a.errOut, "无法确定当前容器；请传 --container NAME 或设置 NEU_BOX_CONTAINER")
		fmt.Fprintln(a.errOut, "为避免销毁 sandbox 时连当前终端一起杀掉，本次未释放。")
		return 2
	}

	payload := map[string]any{"sandbox_name": sandboxName}
	if container != "" {
		payload["container"] = container
		payload["pid"] = a.getPPID()
		payload["client_pid"] = a.getPID()
	}
	fmt.Fprintf(a.out, "[neu-sbox] 释放沙盒: %s...\n", sandboxName)
	status, raw, err := a.api.request(http.MethodPost, "/sandbox/release", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	_ = printJSON(a.out, raw)
	if err := responseError(status, raw); err != nil {
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	if err := a.forgetContainer(sandboxName); err != nil {
		fmt.Fprintf(a.errOut, "警告: 无法删除容器状态文件: %v\n", err)
	}
	fmt.Fprintln(a.out, "\n✓ 沙盒已释放")
	return 0
}

type sandboxRecord struct {
	Name    string   `json:"name"`
	Owner   string   `json:"owner"`
	CPU     int      `json:"cpu"`
	Mem     string   `json:"mem"`
	Devices []string `json:"devices"`
}

type sandboxListResponse struct {
	Sandboxes      []sandboxRecord `json:"sandboxes"`
	CurrentSandbox *string         `json:"current_sandbox"`
}

func (a *app) runList(args []string) int {
	if len(args) != 0 {
		return a.usageError("用法: neu-sbox list")
	}
	fmt.Fprintln(a.out, "[neu-sbox] 所有沙盒:")
	status, raw, err := a.api.request(http.MethodGet, "/sandbox/list", nil, nil)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		_ = printJSON(a.errOut, raw)
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	var response sandboxListResponse
	if err := decodeJSON(raw, &response); err != nil {
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	if len(response.Sandboxes) == 0 {
		fmt.Fprintln(a.out, "  (无)")
		return 0
	}
	for _, sandbox := range response.Sandboxes {
		owner := sandbox.Owner
		if owner == "" {
			owner = "?"
		}
		devices := "—"
		if len(sandbox.Devices) > 0 {
			devices = strings.Join(sandbox.Devices, ",")
		}
		resources := make([]string, 0, 2)
		if sandbox.CPU != 0 {
			resources = append(resources, fmt.Sprintf("CPU=%d", sandbox.CPU))
		}
		if sandbox.Mem != "" && sandbox.Mem != "0" {
			resources = append(resources, "mem="+sandbox.Mem)
		}
		resourceText := "资源不限"
		if len(resources) > 0 {
			resourceText = strings.Join(resources, " ")
		}
		fmt.Fprintf(a.out, "  %s\n", sandbox.Name)
		fmt.Fprintf(a.out, "    用户: %s  |  设备: %s  |  %s\n", owner, devices, resourceText)
	}
	return 0
}

func (a *app) runStatus(args []string) int {
	if len(args) != 0 {
		return a.usageError("用法: neu-sbox status")
	}
	shellPID := a.getPPID()
	if a.config.container != "" {
		query := url.Values{
			"username":  []string{a.config.username},
			"container": []string{a.config.container},
			"pid":       []string{strconv.Itoa(shellPID)},
		}
		status, raw, err := a.api.request(http.MethodGet, "/sandbox/list", query, nil)
		if err != nil {
			return a.requestError(err)
		}
		if err := responseError(status, raw); err != nil {
			_ = printJSON(a.errOut, raw)
			fmt.Fprintln(a.errOut, err)
			return 1
		}
		var response sandboxListResponse
		if err := decodeJSON(raw, &response); err != nil {
			fmt.Fprintln(a.errOut, err)
			return 1
		}
		fmt.Fprintf(a.out, "[neu-sbox] Shell PID=%d container=%s\n", shellPID, a.config.container)
		if response.CurrentSandbox == nil || *response.CurrentSandbox == "" {
			fmt.Fprintln(a.out, "  未在任何沙盒中")
		} else {
			fmt.Fprintf(a.out, "  %s\n", *response.CurrentSandbox)
		}
		return 0
	}

	fmt.Fprintf(a.out, "[neu-sbox] Shell PID=%d\n", shellPID)
	raw, err := a.readFile(fmt.Sprintf("/proc/%d/cgroup", shellPID))
	if err != nil {
		fmt.Fprintln(a.errOut, "  无法读取 cgroup 信息")
		return 1
	}
	found := false
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		if strings.Contains(line, "sandbox_") {
			fmt.Fprintln(a.out, line)
			found = true
		}
	}
	if !found {
		fmt.Fprintln(a.out, "  未在任何沙盒中")
	}
	return 0
}

func (a *app) runJoin(args []string) int {
	if len(args) != 1 || strings.TrimSpace(args[0]) == "" {
		return a.usageError("用法: neu-sbox join <sandbox_name>")
	}
	if a.insideContainer() {
		return a.usageError("容器终端不需要 join：从已 acquire 的 shell 启动子进程即可继承沙盒。")
	}
	sandboxName := strings.TrimSpace(args[0])
	shellPID := a.getPPID()
	payload := map[string]any{
		"username":     a.config.username,
		"pid":          shellPID,
		"sandbox_name": sandboxName,
	}
	fmt.Fprintf(a.out, "[neu-sbox] 加入沙盒: %s\n", sandboxName)
	fmt.Fprintf(a.out, "[neu-sbox] PID=%d user=%s\n", shellPID, a.config.username)
	status, raw, err := a.api.request(http.MethodPost, "/sandbox/join", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	_ = printJSON(a.out, raw)
	if err := responseError(status, raw); err != nil {
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	fmt.Fprintf(a.out, "\n✓ 已加入沙盒 %s\n", sandboxName)
	return 0
}

func (a *app) runTasks(args []string) int {
	if len(args) != 0 {
		return a.usageError("用法: neu-sbox tasks")
	}
	fmt.Fprintln(a.out, "[neu-sbox] 任务队列:")
	status, raw, err := a.api.request(http.MethodGet, "/command/queue", nil, nil)
	if err != nil {
		return a.requestError(err)
	}
	_ = printJSON(a.out, raw)
	if err := responseError(status, raw); err != nil {
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	return 0
}

type taskResult struct {
	ReturnCode *int `json:"returncode"`
	TimedOut   bool `json:"timed_out"`
}

type taskResultResponse struct {
	TaskID     string      `json:"task_id"`
	UserID     string      `json:"user_id"`
	Command    string      `json:"command"`
	Status     string      `json:"status"`
	CPU        int         `json:"cpu"`
	Mem        string      `json:"mem"`
	DeviceNum  int         `json:"device_num"`
	Devices    []string    `json:"devices"`
	CreatedAt  *float64    `json:"created_at"`
	FinishedAt *float64    `json:"finished_at"`
	Result     *taskResult `json:"result"`
}

func (a *app) runResult(args []string) int {
	if len(args) != 1 || strings.TrimSpace(args[0]) == "" {
		return a.usageError("用法: neu-sbox result <task_id>")
	}
	taskID := strings.TrimSpace(args[0])
	pathID := url.PathEscape(taskID)
	status, raw, err := a.api.request(http.MethodGet, "/command/result/"+pathID, nil, nil)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		_ = printJSON(a.errOut, raw)
		fmt.Fprintln(a.errOut, err)
		return 1
	}
	var response taskResultResponse
	if err := decodeJSON(raw, &response); err != nil {
		fmt.Fprintln(a.errOut, err)
		return 1
	}

	logStatus, logRaw, logErr := a.api.request(
		http.MethodGet,
		"/command/result/"+pathID+"/log",
		url.Values{"raw": []string{"1"}},
		nil,
	)
	if logErr != nil || logStatus < 200 || logStatus >= 300 {
		logRaw = nil
	}
	if len(logRaw) > 0 {
		fmt.Fprintln(a.out, strings.TrimRight(string(logRaw), "\r\n"))
	} else {
		fmt.Fprintln(a.out, "(无输出)")
	}

	icons := map[string]string{
		"completed": "✓",
		"failed":    "✗",
		"running":   "▶",
		"queued":    "○",
	}
	icon := icons[response.Status]
	if icon == "" {
		icon = "?"
	}
	var summary strings.Builder
	fmt.Fprintf(&summary, "[%s %s]", icon, response.Status)
	if response.Result != nil && response.Result.ReturnCode != nil {
		fmt.Fprintf(&summary, "  rc=%d", *response.Result.ReturnCode)
		if response.Result.TimedOut {
			summary.WriteString(" (超时)")
		}
	}
	fmt.Fprintf(&summary, "  |  %s  |  %s", response.UserID, response.Command)
	resources := make([]string, 0, 2)
	if response.CPU != 0 {
		resources = append(resources, fmt.Sprintf("CPU=%d", response.CPU))
	}
	if response.Mem != "" && response.Mem != "0" {
		resources = append(resources, "mem="+response.Mem)
	}
	if len(resources) > 0 {
		fmt.Fprintf(&summary, "  |  %s", strings.Join(resources, "  "))
	}
	if response.DeviceNum != 0 {
		fmt.Fprintf(&summary, "  |  设备=%d", response.DeviceNum)
		if len(response.Devices) > 0 {
			fmt.Fprintf(&summary, " (%s)", strings.Join(response.Devices, ","))
		}
	}
	timestamp := response.FinishedAt
	if timestamp == nil {
		timestamp = response.CreatedAt
	}
	if timestamp != nil {
		formatted := time.Unix(int64(*timestamp), 0).Local().Format("01-02 15:04")
		fmt.Fprintf(&summary, "  |  %s", formatted)
	}
	fmt.Fprintf(a.out, "\n%s\n", summary.String())
	return 0
}
