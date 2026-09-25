package cli

import (
	"fmt"
	"net/http"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

// requiredAPIVersion 是本客户端要求的最低 worker API 版本。
// worker 0.4.0 起使用 /tasks 资源接口（API v2）；低于该版本不兼容。
const requiredAPIVersion = 2

// healthResponse 对应 worker GET /healthz。
type healthResponse struct {
	Status     string `json:"status"`
	Role       string `json:"role"`
	APIVersion *int   `json:"api_version"`
	Version    string `json:"version"`
	SchemaVer  any    `json:"schema_version"`
}

// runCheck 检查目标 worker 的可达性与 API 版本兼容性。
func (a *app) runCheck(args []string) int {
	if len(args) != 0 {
		return a.usageError("用法: neubox check")
	}
	status, raw, err := a.worker.Request(http.MethodGet, "/healthz", nil, nil)
	if err != nil {
		return a.requestError(err)
	}
	if status != http.StatusOK {
		fmt.Fprintf(a.errOut, "worker /healthz 返回 HTTP %d\n", status)
		return 1
	}
	var health healthResponse
	if err := api.DecodeJSON(raw, &health); err != nil {
		fmt.Fprintf(a.errOut, "解析 /healthz 失败: %v\n", err)
		return 1
	}
	fmt.Fprintf(a.out, "[neubox] %s %s (schema %v)\n", health.Role, health.Version, health.SchemaVer)
	if health.APIVersion == nil {
		fmt.Fprintln(a.out, "  api_version: 未上报（旧版 worker）")
		fmt.Fprintln(a.errOut, "error: worker 未上报 api_version，不支持 /tasks；请升级 worker")
		return 1
	}
	fmt.Fprintf(a.out, "  api_version: %d (客户端要求 >= %d)\n", *health.APIVersion, requiredAPIVersion)
	if *health.APIVersion < requiredAPIVersion {
		fmt.Fprintln(a.errOut, "error: worker API 版本过低，请升级 worker 或降级客户端")
		return 1
	}
	fmt.Fprintln(a.out, "  ✓ 兼容")
	return 0
}
