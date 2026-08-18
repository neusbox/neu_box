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

type resourceOptions struct {
	deviceIDs    []string
	deviceNum    int
	deviceNumSet bool
	cpu          int
	cpuSet       bool
	memory       int
	memorySet    bool
}

type acquireOptions struct {
	resourceOptions
	pid       int
	pidSet    bool
	container string
}

type submitOptions struct {
	resourceOptions
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
	Error    string `json:"error"`
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
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.container = strings.TrimSpace(raw)
			if options.container == "" {
				return options, errors.New("--container 不能为空")
			}
		case "--command", "--workdir", "--container-user", "--env", "--":
			return options, errors.New("命令任务已移至 submit；请使用 neu-sbox submit [选项] -- <command>")
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
	if err := validateResourceOptions(options.resourceOptions); err != nil {
		return options, err
	}
	return options, nil
}

func (a *app) runSubmit(args []string) int {
	options, err := parseSubmitOptions(args)
	if err != nil {
		return a.usageError(err.Error())
	}
	return a.submitCommand(options)
}

func parseSubmitOptions(args []string) (submitOptions, error) {
	options := submitOptions{environment: make(map[string]string)}

	for index := 0; index < len(args); index++ {
		argument := args[index]
		if argument == "--" {
			if options.command != "" {
				return options, errors.New("submit 只能指定一个命令")
			}
			if index+1 >= len(args) {
				return options, errors.New("submit 缺少命令；请在 -- 后指定命令")
			}
			options.command = joinCommandArguments(args[index+1:])
			break
		}
		if handled, err := consumeResourceOption(args, &index, &options.resourceOptions); handled || err != nil {
			if err != nil {
				return options, err
			}
			continue
		}

		switch argument {
		case "--command":
			if options.command != "" {
				return options, errors.New("submit 只能指定一个命令")
			}
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.command = strings.TrimSpace(raw)
		case "--container":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.container = strings.TrimSpace(raw)
			if options.container == "" {
				return options, errors.New("--container 不能为空")
			}
		case "--workdir":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.workdir = raw
		case "--container-user":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.containerUser = raw
		case "--env":
			raw, err := optionValue(args, &index)
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
				return options, fmt.Errorf("未知 submit 选项: %s", argument)
			}
			return options, fmt.Errorf("无法识别 submit 参数 %q；命令必须放在 -- 后", argument)
		}
	}

	if strings.TrimSpace(options.command) == "" {
		return options, errors.New("submit 缺少命令；请在 -- 后指定命令")
	}
	if options.container == "" && (options.workdir != "" || options.containerUser != "" || len(options.environment) > 0) {
		return options, errors.New("--workdir/--container-user/--env 必须配合 --container")
	}
	if err := validateResourceOptions(options.resourceOptions); err != nil {
		return options, err
	}
	return options, nil
}

func consumeResourceOption(args []string, index *int, options *resourceOptions) (bool, error) {
	argument := args[*index]
	switch argument {
	case "--device":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		deviceID := strings.TrimSpace(raw)
		if deviceID == "" {
			return true, errors.New("--device 不能为空")
		}
		options.deviceIDs = append(options.deviceIDs, deviceID)
	case "--devices":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		count := len(options.deviceIDs)
		for _, item := range strings.Split(raw, ",") {
			if item = strings.TrimSpace(item); item != "" {
				options.deviceIDs = append(options.deviceIDs, item)
			}
		}
		if len(options.deviceIDs) == count {
			return true, errors.New("--devices 不能为空")
		}
	case "--device-num":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		options.deviceNum, err = nonNegativeInteger("--device-num", raw)
		if err != nil {
			return true, err
		}
		options.deviceNumSet = true
	case "--cpu":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		options.cpu, err = nonNegativeInteger("--cpu", raw)
		if err != nil {
			return true, err
		}
		options.cpuSet = true
	case "--mem":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		options.memory, err = nonNegativeInteger("--mem", raw)
		if err != nil {
			return true, err
		}
		options.memorySet = true
	default:
		return false, nil
	}
	if len(options.deviceIDs) > 0 {
		options.deviceNum = 0
	}
	return true, nil
}

func optionValue(args []string, index *int) (string, error) {
	argument := args[*index]
	if *index+1 >= len(args) {
		return "", fmt.Errorf("%s 缺少参数", argument)
	}
	*index++
	return args[*index], nil
}

func applyPositionalResources(options *resourceOptions, positionals []string) error {
	var err error
	if len(positionals) > 0 && !options.deviceNumSet && len(options.deviceIDs) == 0 {
		options.deviceNum, err = nonNegativeInteger("device_num", positionals[0])
		if err != nil {
			return err
		}
	}
	if len(positionals) > 1 && !options.cpuSet {
		options.cpu, err = nonNegativeInteger("cpu", positionals[1])
		if err != nil {
			return err
		}
	}
	if len(positionals) > 2 && !options.memorySet {
		options.memory, err = nonNegativeInteger("mem", positionals[2])
		if err != nil {
			return err
		}
	}
	if len(options.deviceIDs) > 0 {
		options.deviceNum = 0
	}
	return nil
}

func validateResourceOptions(options resourceOptions) error {
	if options.deviceNumSet && len(options.deviceIDs) > 0 {
		return errors.New("--device/--devices 与 --device-num 互斥")
	}
	return nil
}

func joinCommandArguments(arguments []string) string {
	if len(arguments) == 1 {
		return arguments[0]
	}
	quoted := make([]string, 0, len(arguments))
	for _, argument := range arguments {
		quoted = append(quoted, quoteCommandArgument(argument))
	}
	return strings.Join(quoted, " ")
}

func quoteCommandArgument(argument string) string {
	if argument == "" {
		return "''"
	}
	if strings.IndexFunc(argument, func(character rune) bool {
		return !(character >= 'a' && character <= 'z') &&
			!(character >= 'A' && character <= 'Z') &&
			!(character >= '0' && character <= '9') &&
			!strings.ContainsRune("_@%+=:,./-", character)
	}) == -1 {
		return argument
	}
	return "'" + strings.ReplaceAll(argument, "'", "'\"'\"'") + "'"
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

func (a *app) submitCommand(options submitOptions) int {
	payload := commandRequest{
		UserID:    a.config.username,
		Command:   options.command,
		DeviceNum: options.deviceNum,
		DeviceIDs: options.deviceIDs,
		CPU:       options.cpu,
		Memory:    options.memory,
		MemUnit:   "GB",
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

	status, raw, err := a.api.request(http.MethodPost, "/command/run", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	var response commandResponse
	if err := decodeJSON(raw, &response); err != nil || response.TaskID == "" {
		if err == nil {
			err = errors.New("Worker 响应缺少 task_id")
		}
		return a.internalError("invalid_worker_response", err)
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	fmt.Fprintln(a.out, "[neu-sbox] 任务已提交")
	fmt.Fprintf(a.out, "    ID=%s\n", response.TaskID)
	fmt.Fprintf(a.out, "    queue_position: #%d\n", response.Position)
	fmt.Fprintf(a.out, "    command: %s\n", options.command)
	fmt.Fprintf(a.out, "    result: neu-sbox result %s\n", response.TaskID)
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

	status, raw, err := a.api.request(http.MethodPost, "/sandbox/acquire", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	var response acquireResponse
	if err := decodeJSON(raw, &response); err != nil || response.SandboxName == "" {
		if err == nil {
			err = errors.New("Worker 响应缺少 sandbox_name")
		}
		return a.internalError("invalid_worker_response", err)
	}
	if container != "" {
		if err := a.rememberContainer(response.SandboxName, container); err != nil {
			a.printWarning(
				"container_state_write_failed",
				fmt.Sprintf("无法保存容器标识；release 时请传 --container %s: %v", container, err),
			)
		}
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	devices := "无"
	if len(response.Devices) > 0 {
		devices = strings.Join(response.Devices, ",")
	}
	fmt.Fprintln(a.out, "[neu-sbox] 沙盒已创建")
	fmt.Fprintf(a.out, "    sandbox: %s\n", response.SandboxName)
	fmt.Fprintf(a.out, "    pid: %d\n", shellPID)
	fmt.Fprintf(a.out, "    devices: %s\n", devices)
	if container != "" {
		fmt.Fprintf(a.out, "    container: %s\n", container)
	}
	fmt.Fprintf(a.out, "    release: neu-sbox release %s\n", response.SandboxName)
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
			a.printWarning("container_state_read_failed", fmt.Sprintf("无法读取保存的容器标识: %v", err))
		}
	}
	if a.insideContainer() && container == "" {
		return a.usageError("无法确定当前容器；请传 --container NAME 或设置 NEU_BOX_CONTAINER。为避免销毁当前终端，本次未释放")
	}

	payload := map[string]any{"sandbox_name": sandboxName}
	if container != "" {
		payload["container"] = container
		payload["pid"] = a.getPPID()
		payload["client_pid"] = a.getPID()
	}
	status, raw, err := a.api.request(http.MethodPost, "/sandbox/release", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	if err := a.forgetContainer(sandboxName); err != nil {
		a.printWarning("container_state_delete_failed", fmt.Sprintf("无法删除容器状态文件: %v", err))
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	fmt.Fprintln(a.out, "[neu-sbox] 沙盒已释放")
	fmt.Fprintf(a.out, "    sandbox: %s\n", sandboxName)
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
	status, raw, err := a.api.request(http.MethodGet, "/sandbox/list", nil, nil)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	var response sandboxListResponse
	if err := decodeJSON(raw, &response); err != nil {
		return a.internalError("invalid_worker_response", err)
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	fmt.Fprintln(a.out, "[neu-sbox] 沙盒列表")
	if len(response.Sandboxes) == 0 {
		fmt.Fprintln(a.out, "    (无)")
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
		fmt.Fprintf(a.out, "    %s\n", sandbox.Name)
		fmt.Fprintf(a.out, "        用户: %s  |  设备: %s  |  %s\n", owner, devices, resourceText)
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
			return a.workerFailure(status, raw)
		}
		var response sandboxListResponse
		if err := decodeJSON(raw, &response); err != nil {
			return a.internalError("invalid_worker_response", err)
		}
		sandboxName := ""
		if response.CurrentSandbox != nil {
			sandboxName = *response.CurrentSandbox
		}
		return a.printShellStatus(shellPID, a.config.container, sandboxName)
	}

	raw, err := a.readFile(fmt.Sprintf("/proc/%d/cgroup", shellPID))
	if err != nil {
		return a.internalError("cgroup_read_failed", errors.New("无法读取 cgroup 信息"))
	}
	return a.printShellStatus(shellPID, "", sandboxNameFromCgroup(raw))
}

func (a *app) printShellStatus(shellPID int, container, sandboxName string) int {
	if a.jsonOutput {
		var containerValue any
		if container != "" {
			containerValue = container
		}
		var sandboxValue any
		if sandboxName != "" {
			sandboxValue = sandboxName
		}
		_ = printJSONValue(a.out, map[string]any{
			"container": containerValue,
			"pid":       shellPID,
			"sandbox":   sandboxValue,
		})
		return 0
	}
	fmt.Fprintln(a.out, "[neu-sbox] Shell 状态")
	fmt.Fprintf(a.out, "    pid: %d\n", shellPID)
	if container != "" {
		fmt.Fprintf(a.out, "    container: %s\n", container)
	}
	if sandboxName == "" {
		fmt.Fprintln(a.out, "    sandbox: none")
	} else {
		fmt.Fprintf(a.out, "    sandbox: %s\n", sandboxName)
	}
	return 0
}

func sandboxNameFromCgroup(raw []byte) string {
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		marker := strings.Index(line, "sandbox_")
		if marker < 0 {
			continue
		}
		name := line[marker+len("sandbox_"):]
		if separator := strings.IndexRune(name, '/'); separator >= 0 {
			name = name[:separator]
		}
		if name != "" {
			return name
		}
	}
	return ""
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
	status, raw, err := a.api.request(http.MethodPost, "/sandbox/join", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	fmt.Fprintln(a.out, "[neu-sbox] 已加入沙盒")
	fmt.Fprintf(a.out, "    sandbox: %s\n", sandboxName)
	fmt.Fprintf(a.out, "    pid: %d\n", shellPID)
	return 0
}

type taskQueueResponse struct {
	Queue        []taskResultResponse `json:"queue"`
	TotalPending int                  `json:"total_pending"`
}

func (a *app) runTasks(args []string) int {
	if len(args) != 0 {
		return a.usageError("用法: neu-sbox tasks")
	}
	status, raw, err := a.api.request(http.MethodGet, "/command/queue", nil, nil)
	if err != nil {
		return a.requestError(err)
	}
	if err := responseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	var response taskQueueResponse
	if err := decodeJSON(raw, &response); err != nil {
		return a.internalError("invalid_worker_response", err)
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	fmt.Fprintln(a.out, "[neu-sbox] 任务列表")
	fmt.Fprintf(a.out, "    total: %d\n", len(response.Queue))
	fmt.Fprintf(a.out, "    pending: %d\n", response.TotalPending)
	if len(response.Queue) == 0 {
		fmt.Fprintln(a.out, "    (无)")
		return 0
	}
	for _, task := range response.Queue {
		fmt.Fprintln(a.out)
		fmt.Fprintf(a.out, "    %s\n", task.TaskID)
		fmt.Fprintf(a.out, "        status: %s\n", task.Status)
		fmt.Fprintf(a.out, "        user: %s\n", task.UserID)
		fmt.Fprintf(a.out, "        command: %s\n", task.Command)
		if task.Status == "queued" && task.Position > 0 {
			fmt.Fprintf(a.out, "        position: #%d\n", task.Position)
		}
		if resources := taskResourceText(task); resources != "" {
			fmt.Fprintf(a.out, "        resources: %s\n", resources)
		}
	}
	return 0
}

type taskResult struct {
	ReturnCode *int `json:"returncode"`
	TimedOut   bool `json:"timed_out"`
	Error      any  `json:"error"`
}

type taskResultResponse struct {
	TaskID     string      `json:"task_id"`
	UserID     string      `json:"user_id"`
	Command    string      `json:"command"`
	Status     string      `json:"status"`
	Position   int         `json:"position"`
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
		return a.workerFailure(status, raw)
	}
	var response taskResultResponse
	if err := decodeJSON(raw, &response); err != nil {
		return a.internalError("invalid_worker_response", err)
	}

	logStatus, logRaw, logErr := a.api.request(
		http.MethodGet,
		"/command/result/"+pathID+"/log",
		url.Values{"raw": []string{"1"}},
		nil,
	)
	if logErr != nil {
		return a.requestError(fmt.Errorf("获取任务日志: %w", logErr))
	}
	if err := responseError(logStatus, logRaw); err != nil {
		return a.workerFailure(logStatus, logRaw)
	}

	if a.jsonOutput {
		var output map[string]any
		if err := decodeJSON(raw, &output); err != nil {
			return a.internalError("invalid_worker_response", err)
		}
		output["log"] = string(logRaw)
		_ = printJSONValue(a.out, output)
		return 0
	}

	fmt.Fprintln(a.out, "[neu-sbox] 任务日志")
	if len(logRaw) > 0 {
		fmt.Fprintln(a.out, strings.TrimRight(string(logRaw), "\r\n"))
	} else {
		fmt.Fprintln(a.out, "    (暂无日志)")
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
	fmt.Fprintln(a.out)
	fmt.Fprintln(a.out, "[neu-sbox] 任务结果")
	fmt.Fprintf(a.out, "    ID=%s\n", response.TaskID)
	fmt.Fprintf(a.out, "    status: [%s %s]\n", icon, response.Status)
	if response.Result != nil && response.Result.ReturnCode != nil {
		fmt.Fprintf(a.out, "    rc=%d\n", *response.Result.ReturnCode)
		if response.Result.TimedOut {
			fmt.Fprintln(a.out, "    timed_out: true")
		}
		if response.Result.Error != nil {
			fmt.Fprintf(a.out, "    error: %v\n", response.Result.Error)
		}
	}
	fmt.Fprintf(a.out, "    user: %s\n", response.UserID)
	fmt.Fprintf(a.out, "    command: %s\n", response.Command)
	resources := taskResourceText(response)
	if resources != "" {
		fmt.Fprintf(a.out, "    resources: %s\n", resources)
	}
	timestamp := response.FinishedAt
	if timestamp == nil {
		timestamp = response.CreatedAt
	}
	if timestamp != nil {
		formatted := time.Unix(int64(*timestamp), 0).Local().Format("01-02 15:04")
		fmt.Fprintf(a.out, "    time: %s\n", formatted)
	}
	return 0
}

func taskResourceText(task taskResultResponse) string {
	resources := make([]string, 0, 3)
	if task.CPU != 0 {
		resources = append(resources, fmt.Sprintf("CPU=%d", task.CPU))
	}
	if task.Mem != "" && task.Mem != "0" {
		resources = append(resources, "mem="+task.Mem)
	}
	if task.DeviceNum != 0 {
		devices := fmt.Sprintf("设备=%d", task.DeviceNum)
		if len(task.Devices) > 0 {
			devices += " (" + strings.Join(task.Devices, ",") + ")"
		}
		resources = append(resources, devices)
	}
	return strings.Join(resources, "  ")
}
