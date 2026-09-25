package cli

import (
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

// runCancel 是 `neubox cancel <id> [--kind task|acquire]`：统一取消入口。
//
// 任务和 acquire 会话共用 Worker 的 `DELETE /tasks/<id>?kind=`：排队中的条目被摘出
// 队列（任务留痕为 cancelled），运行中的任务发取消信号，已经拿到卡的 acquire 就地
// 释放。host_pid 是给 acquire 用的 —— 取消请求可能来自沙盒内部的进程，Worker 得先
// 把它搬出去再销毁，否则 cgroup.kill 会连调用方一起带走。
func (a *app) runCancel(args []string) int {
	kind := "task"
	identifier := ""
	for index := 0; index < len(args); index++ {
		argument := args[index]
		switch argument {
		case "--kind":
			value, err := optionValue(args, &index)
			if err != nil {
				return a.usageError(err.Error())
			}
			kind = strings.TrimSpace(value)
		default:
			if strings.HasPrefix(argument, "-") {
				return a.usageError("未知 cancel 选项: " + argument)
			}
			if identifier != "" {
				return a.usageError(cancelUsage)
			}
			identifier = strings.TrimSpace(argument)
		}
	}
	if identifier == "" {
		return a.usageError(cancelUsage)
	}
	if kind != "task" && kind != "acquire" {
		return a.usageError("--kind 只能是 task 或 acquire")
	}

	status, raw, err := a.worker.Request(
		http.MethodDelete,
		"/tasks/"+url.PathEscape(identifier)+"?kind="+url.QueryEscape(kind),
		nil,
		map[string]any{"host_pid": a.getPID()},
	)
	if err != nil {
		return a.requestError(err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	var response struct {
		Status      string `json:"status"`
		SandboxName string `json:"sandbox_name"`
	}
	if err := api.DecodeJSON(raw, &response); err != nil {
		return a.internalError("invalid_worker_response", err)
	}
	a.printCancelOutcome(kind, identifier, response.Status, response.SandboxName)
	return 0
}

const cancelUsage = "用法: neubox cancel <id> [--kind task|acquire]"

func (a *app) printCancelOutcome(kind, identifier, status, sandboxName string) {
	fmt.Fprintln(a.out, "[neubox] 已取消")
	fmt.Fprintf(a.out, "    id: %s\n", identifier)
	fmt.Fprintf(a.out, "    kind: %s\n", kind)
	fmt.Fprintf(a.out, "    status: %s\n", status)
	if sandboxName != "" {
		fmt.Fprintf(a.out, "    sandbox: %s（已释放）\n", sandboxName)
	}
}
