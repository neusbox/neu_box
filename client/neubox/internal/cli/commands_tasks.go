package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

type taskQueueResponse struct {
	Queue        []json.RawMessage `json:"queue"`
	TotalPending int               `json:"total_pending"`
}

// defaultTasksWindow 是 tasks 默认视图的时间窗：已完成/失败的任务只展示
// finished_at 在窗口内的；活跃任务（queued/running）不受窗口限制。
const defaultTasksWindow = 2 * time.Hour

type tasksOptions struct {
	all   bool
	since time.Duration
}

func parseTasksOptions(args []string) (tasksOptions, error) {
	options := tasksOptions{since: defaultTasksWindow}
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "--all":
			options.all = true
		case "--since":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			duration, err := time.ParseDuration(raw)
			if err != nil || duration <= 0 {
				return options, fmt.Errorf("--since 必须是正 duration，例如 30m / 6h: %q", raw)
			}
			options.since = duration
		default:
			return options, fmt.Errorf("未知 tasks 选项: %s", args[index])
		}
	}
	return options, nil
}

func (a *app) runTasks(args []string) int {
	options, err := parseTasksOptions(args)
	if err != nil {
		return a.usageError(err.Error())
	}
	status, raw, err := a.worker.Request(
		http.MethodGet,
		"/tasks",
		nil,
		nil,
	)
	if err != nil {
		return a.requestError(err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	var response taskQueueResponse
	if err := api.DecodeJSON(raw, &response); err != nil {
		return a.internalError("invalid_worker_response", err)
	}
	visible, hidden := filterTaskList(response.Queue, options)

	if a.jsonOutput {
		if hidden == 0 {
			// 没有过滤掉任何条目：直接透传 Worker 原始响应。
			_ = printJSON(a.out, raw)
		} else {
			// RawMessage 保留 Worker 返回的全部字段（eta/priority/target 等）。
			_ = printJSONValue(a.out, struct {
				Queue        []json.RawMessage `json:"queue"`
				TotalPending int               `json:"total_pending"`
			}{visible, response.TotalPending})
		}
		return 0
	}
	fmt.Fprintln(a.out, "[neubox] 任务列表")
	fmt.Fprintf(a.out, "    total: %d\n", len(visible))
	fmt.Fprintf(a.out, "    pending: %d\n", response.TotalPending)
	if hidden > 0 {
		fmt.Fprintf(a.out, "    已省略 %d 个更早结束的任务（--all 查看全部）\n", hidden)
	}
	tasks := make([]taskResultResponse, 0, len(visible))
	for _, entry := range visible {
		var task taskResultResponse
		if err := api.DecodeJSON(entry, &task); err != nil {
			continue
		}
		tasks = append(tasks, task)
	}
	if len(tasks) == 0 {
		fmt.Fprintln(a.out, "    (无)")
		return 0
	}
	for _, task := range tasks {
		fmt.Fprintln(a.out)
		fmt.Fprintf(a.out, "    %s\n", entryID(task))
		fmt.Fprintf(a.out, "        kind: %s\n", entryKind(task))
		fmt.Fprintf(a.out, "        status: %s\n", task.Status)
		fmt.Fprintf(a.out, "        user: %s\n", task.UserID)
		if entryKind(task) == "acquire" {
			// acquire 是会话不是命令任务：没有 command / 日志 / 退出码。
			if task.PID != 0 {
				fmt.Fprintf(a.out, "        pid: %d\n", task.PID)
			}
			if task.Sandbox != "" {
				fmt.Fprintf(a.out, "        sandbox: %s\n", task.Sandbox)
			}
		} else {
			fmt.Fprintf(a.out, "        command: %s\n", task.Command)
		}
		if task.Position > 0 {
			fmt.Fprintf(a.out, "        position: #%d\n", task.Position)
		}
		if resources := taskResourceText(task); resources != "" {
			fmt.Fprintf(a.out, "        resources: %s\n", resources)
		}
	}
	return 0
}

// filterTaskList 实现 tasks 默认视图：
//   - 活跃条目（status 为 queued/running，含排队中的 acquire 会话）永远保留；
//   - 已结束条目按 finished_at（缺失时退回 created_at）是否在窗口内决定；
//   - 无法解析或没有时间的条目不隐藏（宁多勿漏）。
//
// 返回保留的原始条目（JSON 输出时不丢 worker 额外字段）和被隐藏的条目数。
func filterTaskList(queue []json.RawMessage, options tasksOptions) ([]json.RawMessage, int) {
	if options.all {
		return queue, 0
	}
	cutoff := time.Now().Add(-options.since)
	visible := make([]json.RawMessage, 0, len(queue))
	for _, entry := range queue {
		if taskEntryVisible(entry, cutoff) {
			visible = append(visible, entry)
		}
	}
	return visible, len(queue) - len(visible)
}

func taskEntryVisible(entry json.RawMessage, cutoff time.Time) bool {
	var probe struct {
		Status     string   `json:"status"`
		CreatedAt  *float64 `json:"created_at"`
		FinishedAt *float64 `json:"finished_at"`
	}
	if err := json.Unmarshal(entry, &probe); err != nil {
		return true
	}
	if probe.Status == "queued" || probe.Status == "running" {
		return true
	}
	timestamp := probe.FinishedAt
	if timestamp == nil {
		timestamp = probe.CreatedAt
	}
	if timestamp == nil {
		return true
	}
	return time.Unix(int64(*timestamp), 0).After(cutoff)
}

// entryKind / entryID 兼容两类条目：任务用 task_id，acquire 会话用 request_id。
// 老 Worker 不带 kind/id 字段时按任务处理。
func entryKind(entry taskResultResponse) string {
	if entry.Kind != "" {
		return entry.Kind
	}
	return "task"
}

func entryID(entry taskResultResponse) string {
	if entry.ID != "" {
		return entry.ID
	}
	return entry.TaskID
}

type taskResult struct {
	ReturnCode *int `json:"returncode"`
	TimedOut   bool `json:"timed_out"`
	Error      any  `json:"error"`
}

type taskResultResponse struct {
	TaskID     string      `json:"task_id"`
	Kind       string      `json:"kind"`
	ID         string      `json:"id"`
	UserID     string      `json:"user_id"`
	Command    string      `json:"command"`
	Status     string      `json:"status"`
	Position   int         `json:"position"`
	PID        int         `json:"pid"`
	Sandbox    string      `json:"sandbox_name"`
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
		return a.usageError("用法: neubox result <task_id>")
	}
	taskID := strings.TrimSpace(args[0])
	pathID := url.PathEscape(taskID)
	status, raw, err := a.worker.Request(http.MethodGet, "/tasks/"+pathID, nil, nil)
	if err != nil {
		return a.requestError(err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	var response taskResultResponse
	if err := api.DecodeJSON(raw, &response); err != nil {
		return a.internalError("invalid_worker_response", err)
	}

	logStatus, logRaw, logErr := a.worker.Request(
		http.MethodGet,
		"/tasks/"+pathID+"/log",
		url.Values{"raw": []string{"1"}},
		nil,
	)
	if logErr != nil {
		return a.requestError(fmt.Errorf("获取任务日志: %w", logErr))
	}
	if err := api.ResponseError(logStatus, logRaw); err != nil {
		return a.workerFailure(logStatus, logRaw)
	}

	if a.jsonOutput {
		var output map[string]any
		if err := api.DecodeJSON(raw, &output); err != nil {
			return a.internalError("invalid_worker_response", err)
		}
		output["log"] = string(logRaw)
		_ = printJSONValue(a.out, output)
		return 0
	}

	fmt.Fprintln(a.out, "[neubox] 任务日志")
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
	fmt.Fprintln(a.out, "[neubox] 任务结果")
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
