package cli

import (
	"fmt"
)

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
	fmt.Fprintf(a.errOut, "[neubox] %s\n", message)
}

func (a *app) printWarning(code, message string) {
	if a.jsonOutput {
		_ = printJSONValue(a.errOut, map[string]any{
			"code":    code,
			"warning": message,
		})
		return
	}
	fmt.Fprintf(a.errOut, "[neubox] 警告: %s\n", message)
}
