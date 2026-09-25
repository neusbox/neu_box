package cli

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestResultPrintsLogAndSummary(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/tasks/abc123":
			returnCode := 0
			writeJSON(t, writer, http.StatusOK, map[string]any{
				"task_id":     "abc123",
				"user_id":     "yuxd",
				"command":     "echo ok",
				"status":      "completed",
				"cpu":         2,
				"mem":         "4G",
				"device_num":  1,
				"devices":     []string{"235:0"},
				"finished_at": 1_700_000_000,
				"result": map[string]any{
					"returncode": returnCode,
					"timed_out":  false,
				},
			})
		case "/tasks/abc123/log":
			_, _ = io.WriteString(writer, "hello\n")
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL)
	code := application.run([]string{"result", "abc123"})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	for _, expected := range []string{"hello", "[✓ completed]", "rc=0", "设备=1"} {
		if !strings.Contains(out.String(), expected) {
			t.Fatalf("missing %q in %s", expected, out.String())
		}
	}
}

// tasksQueueFixture 覆盖默认视图的所有分支：
//   - run-1  运行中，创建已超窗 → 必须保留（活跃任务不受窗口限制）
//   - que-1  排队中（带 eta，验证 JSON 模式字段透传）
//   - ok-1   30 分钟前完成 → 默认保留
//   - old-1  40 小时前完成 → 默认隐藏，--all 显示
//   - notime 无时间戳的旧条目 → 不隐藏（宁多勿漏）
func tasksQueueFixture(now float64) map[string]any {
	return map[string]any{
		"total_pending": 1,
		"queue": []map[string]any{
			{
				"task_id": "run-1", "user_id": "yuxd", "command": "python long.py",
				"status": "running", "created_at": now - 10*3600,
			},
			{
				"task_id": "que-1", "user_id": "yuxd", "command": "echo hi",
				"status": "queued", "position": 1, "eta": 15,
				"created_at": now - 3600,
			},
			{
				"task_id": "ok-1", "user_id": "yuxd", "command": "echo ok",
				"status": "completed", "created_at": now - 7200,
				"finished_at": now - 1800,
			},
			{
				"task_id": "old-1", "user_id": "yuxd", "command": "echo old",
				"status": "failed", "created_at": now - 50*3600,
				"finished_at": now - 40*3600,
			},
			{
				"task_id": "notime-1", "user_id": "yuxd", "command": "echo ?",
				"status": "completed",
			},
		},
	}
}

func tasksQueueServer(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/tasks" {
			http.NotFound(writer, request)
			return
		}
		writeJSON(t, writer, http.StatusOK, tasksQueueFixture(float64(time.Now().Unix())))
	}))
	t.Cleanup(server.Close)
	return server
}

func TestTasksDefaultHidesFinishedOutsideWindow(t *testing.T) {
	application, out, _ := testApplication(tasksQueueServer(t).URL)
	if code := application.run([]string{"tasks"}); code != 0 {
		t.Fatalf("exit=%d", code)
	}
	text := out.String()
	for _, visible := range []string{"run-1", "que-1", "ok-1", "notime-1"} {
		if !strings.Contains(text, visible) {
			t.Fatalf("expected %s in output:\n%s", visible, text)
		}
	}
	if strings.Contains(text, "old-1") {
		t.Fatalf("old-1 should be hidden by default:\n%s", text)
	}
	if !strings.Contains(text, "已省略 1 个更早结束的任务") {
		t.Fatalf("missing omitted hint:\n%s", text)
	}
}

func TestTasksAllShowsEveryEntry(t *testing.T) {
	application, out, _ := testApplication(tasksQueueServer(t).URL)
	if code := application.run([]string{"tasks", "--all"}); code != 0 {
		t.Fatalf("exit=%d", code)
	}
	text := out.String()
	for _, visible := range []string{"run-1", "que-1", "ok-1", "old-1", "notime-1"} {
		if !strings.Contains(text, visible) {
			t.Fatalf("expected %s in --all output:\n%s", visible, text)
		}
	}
	if strings.Contains(text, "已省略") {
		t.Fatalf("--all must not report omitted entries:\n%s", text)
	}
}

func TestTasksSinceWindow(t *testing.T) {
	application, out, _ := testApplication(tasksQueueServer(t).URL)
	if code := application.run([]string{"tasks", "--since", "4h"}); code != 0 {
		t.Fatalf("exit=%d", code)
	}
	text := out.String()
	for _, visible := range []string{"run-1", "que-1", "ok-1", "notime-1"} {
		if !strings.Contains(text, visible) {
			t.Fatalf("expected %s with --since 4h:\n%s", visible, text)
		}
	}
	if strings.Contains(text, "old-1") {
		t.Fatalf("old-1 (40h ago) should stay hidden with --since 4h:\n%s", text)
	}
}

func TestTasksJSONAppliesWindowFilterAndKeepsRawFields(t *testing.T) {
	application, out, _ := testApplication(tasksQueueServer(t).URL)
	if code := application.run([]string{"--json", "tasks"}); code != 0 {
		t.Fatalf("exit=%d", code)
	}
	var response struct {
		Queue        []map[string]any `json:"queue"`
		TotalPending int              `json:"total_pending"`
	}
	if err := json.Unmarshal(out.Bytes(), &response); err != nil {
		t.Fatalf("decode output: %v", err)
	}
	if response.TotalPending != 1 {
		t.Fatalf("total_pending = %d, want 1", response.TotalPending)
	}
	ids := make([]string, 0, len(response.Queue))
	for _, entry := range response.Queue {
		ids = append(ids, entry["task_id"].(string))
		// eta 是客户端未建模的 worker 字段，过滤后仍须保留
		if entry["task_id"] == "que-1" && entry["eta"] == nil {
			t.Fatalf("eta field lost in filtered JSON output: %v", entry)
		}
	}
	if len(ids) != 4 {
		t.Fatalf("queue = %v, want 4 entries (old-1 filtered)", ids)
	}
	for _, id := range ids {
		if id == "old-1" {
			t.Fatalf("old-1 must be filtered in JSON output: %v", ids)
		}
	}
}

func TestTasksJSONAllPassesRawThrough(t *testing.T) {
	application, out, _ := testApplication(tasksQueueServer(t).URL)
	if code := application.run([]string{"--json", "tasks", "--all"}); code != 0 {
		t.Fatalf("exit=%d", code)
	}
	var response struct {
		Queue []map[string]any `json:"queue"`
	}
	if err := json.Unmarshal(out.Bytes(), &response); err != nil {
		t.Fatalf("decode output: %v", err)
	}
	if len(response.Queue) != 5 {
		t.Fatalf("queue length = %d, want 5", len(response.Queue))
	}
}

func TestTasksUnknownOptionRejected(t *testing.T) {
	application, _, errOut := testApplication("http://127.0.0.1:0")
	if code := application.run([]string{"tasks", "--bogus"}); code != 2 {
		t.Fatalf("exit=%d, want 2 (stderr=%s)", code, errOut.String())
	}
	if !strings.Contains(errOut.String(), "未知 tasks 选项") {
		t.Fatalf("missing option error: %s", errOut.String())
	}
}

func TestTasksBadSinceRejected(t *testing.T) {
	application, _, errOut := testApplication("http://127.0.0.1:0")
	if code := application.run([]string{"tasks", "--since", "yesterday"}); code != 2 {
		t.Fatalf("exit=%d, want 2 (stderr=%s)", code, errOut.String())
	}
	if !strings.Contains(errOut.String(), "--since") {
		t.Fatalf("missing since error: %s", errOut.String())
	}
}
