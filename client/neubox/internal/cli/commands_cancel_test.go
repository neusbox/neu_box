package cli

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCancelSendsUnifiedEntryRequest(t *testing.T) {
	var method, path string
	var body map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method, path = r.Method, r.URL.RequestURI()
		_ = json.NewDecoder(r.Body).Decode(&body)
		writeJSON(t, w, http.StatusOK, map[string]any{
			"status": "released", "sandbox_name": "sbx_yuxd_1.slice",
			"kind": "acquire", "id": "abc123",
		})
	}))
	defer server.Close()
	application, out, errOut := testApplication(server.URL)

	if rc := application.run([]string{"cancel", "abc123", "--kind", "acquire"}); rc != 0 {
		t.Fatalf("rc=%d stderr=%s", rc, errOut.String())
	}
	if method != http.MethodDelete {
		t.Fatalf("method=%s", method)
	}
	if path != "/tasks/abc123?kind=acquire" {
		t.Fatalf("path=%s", path)
	}
	if pid, ok := body["host_pid"].(float64); !ok || int(pid) != 222 {
		t.Fatalf("host_pid 应为调用方自己的 PID 222，实际 %v", body["host_pid"])
	}
	if !strings.Contains(out.String(), "已取消") ||
		!strings.Contains(out.String(), "sbx_yuxd_1.slice") {
		t.Fatalf("输出: %s", out.String())
	}
}

func TestCancelDefaultsToTaskKind(t *testing.T) {
	var path string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.RequestURI()
		writeJSON(t, w, http.StatusOK, map[string]any{"status": "cancelled"})
	}))
	defer server.Close()
	application, _, errOut := testApplication(server.URL)

	if rc := application.run([]string{"cancel", "t1"}); rc != 0 {
		t.Fatalf("rc=%d stderr=%s", rc, errOut.String())
	}
	if path != "/tasks/t1?kind=task" {
		t.Fatalf("path=%s", path)
	}
}

func TestCancelRejectsBadArguments(t *testing.T) {
	application, _, _ := testApplication("http://127.0.0.1:1")
	for _, args := range [][]string{
		{"cancel"},
		{"cancel", "x", "--kind", "bogus"},
		{"cancel", "x", "--nope"},
	} {
		if rc := application.run(args); rc != 2 {
			t.Fatalf("%v: 期望用法错误(2)，实际 %d", args, rc)
		}
	}
}
