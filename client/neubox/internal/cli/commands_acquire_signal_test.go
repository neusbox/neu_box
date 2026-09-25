package cli

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"syscall"
	"testing"
	"time"
)

// SIGINT 在"请求飞行途中"到达 —— Worker 已经把它排进队列（真机上这一刻
// `GET /tasks?kind=acquire` 就能看到 queued），客户端还没开始轮询。
//
// 这是真机用例 66 抓到的窗口：handler 原来只在轮询开始时才装，信号落在这个
// 窗口里会走 Go 的默认动作，进程被直接打死（退出码 -2），既不取消也不按 130
// 退出，队列里留一条没人管的请求。
func TestAcquireCancelsOnSIGINTDuringTheRequest(t *testing.T) {
	inRequest := make(chan struct{})
	letResponseGo := make(chan struct{})
	var deletedPath string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/sandbox/acquire":
			close(inRequest)
			<-letResponseGo // 把 SIGINT 钉在"请求已经到 Worker、响应还没回来"这段
			writeJSON(t, w, http.StatusAccepted, map[string]any{
				"acquire_id": "req-late", "status": "queued",
			})
		case r.Method == http.MethodDelete:
			deletedPath = r.URL.RequestURI()
			writeJSON(t, w, http.StatusOK, map[string]any{"status": "cancelled"})
		default:
			t.Errorf("意外请求: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()
	application, _, errOut := testApplication(server.URL)

	done := make(chan int, 1)
	go func() {
		done <- application.run([]string{"acquire", "--device-num", "1"})
	}()

	select {
	case <-inRequest:
	case <-time.After(5 * time.Second):
		t.Fatal("acquire 没有发出请求")
	}
	if err := syscall.Kill(syscall.Getpid(), syscall.SIGINT); err != nil {
		t.Fatalf("发送 SIGINT 失败: %v", err)
	}
	close(letResponseGo)

	select {
	case code := <-done:
		if code != 130 {
			t.Fatalf("Ctrl-C 后应退出 130，实际 %d（stderr=%s）", code, errOut.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("acquire 在 SIGINT 之后没有退出")
	}
	if !strings.Contains(deletedPath, "req-late") ||
		!strings.Contains(deletedPath, "kind=acquire") {
		t.Fatalf("请求飞行途中的 Ctrl-C 没有发出取消请求，deletedPath=%q", deletedPath)
	}
}

// 同一窗口里如果 Worker 直接给了卡（201，不用排队）：Ctrl-C 的意图是"不要了"，
// 客户端必须把刚建的沙盒释放掉再按 130 退出，不能把一张卡留在场上。
func TestAcquireReleasesSandboxOnSIGINTDuringTheRequest(t *testing.T) {
	inRequest := make(chan struct{})
	letResponseGo := make(chan struct{})
	var releasedBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/sandbox/acquire":
			close(inRequest)
			<-letResponseGo
			writeJSON(t, w, http.StatusCreated, map[string]any{
				"sandbox_name": "sbx_test_123.slice",
				"devices":      []string{"234:0"},
			})
		case r.Method == http.MethodPost && r.URL.Path == "/sandbox/release":
			_ = json.NewDecoder(r.Body).Decode(&releasedBody)
			writeJSON(t, w, http.StatusOK, map[string]any{"status": "released"})
		default:
			t.Errorf("意外请求: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()
	application, _, errOut := testApplication(server.URL)

	done := make(chan int, 1)
	go func() {
		done <- application.run([]string{"acquire", "--device-num", "1"})
	}()

	select {
	case <-inRequest:
	case <-time.After(5 * time.Second):
		t.Fatal("acquire 没有发出请求")
	}
	if err := syscall.Kill(syscall.Getpid(), syscall.SIGINT); err != nil {
		t.Fatalf("发送 SIGINT 失败: %v", err)
	}
	close(letResponseGo)

	select {
	case code := <-done:
		if code != 130 {
			t.Fatalf("Ctrl-C 后应退出 130，实际 %d（stderr=%s）", code, errOut.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("acquire 在 SIGINT 之后没有退出")
	}
	if name, _ := releasedBody["sandbox_name"].(string); name != "sbx_test_123.slice" {
		t.Fatalf("刚创建的沙盒没有被释放，release body=%v", releasedBody)
	}
}

// 排队期间按 Ctrl-C：只发一次取消请求（Worker 内部决定"摘出队列"还是"就地释放"），
// 然后按约定退出 130 —— 不需要客户端再补一次 release。
func TestAcquireCancelsOnSIGINTWhileQueued(t *testing.T) {
	polled := make(chan struct{}, 4)
	var deletedPath string
	var deletedBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/sandbox/acquire":
			writeJSON(t, w, http.StatusAccepted, map[string]any{
				"acquire_id": "req-1", "status": "queued",
			})
		case r.Method == http.MethodGet && r.URL.Path == "/sandbox/acquire/req-1":
			select {
			case polled <- struct{}{}:
			default:
			}
			writeJSON(t, w, http.StatusAccepted, map[string]any{"status": "queued"})
		case r.Method == http.MethodDelete:
			deletedPath = r.URL.RequestURI()
			_ = json.NewDecoder(r.Body).Decode(&deletedBody)
			writeJSON(t, w, http.StatusOK, map[string]any{"status": "cancelled"})
		default:
			t.Errorf("意外请求: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()
	application, out, errOut := testApplication(server.URL)

	done := make(chan int, 1)
	go func() {
		done <- application.run([]string{"acquire", "--device-num", "1"})
	}()

	select {
	case <-polled: // 已经开始轮询 → 信号 handler 已经装好
	case <-time.After(5 * time.Second):
		t.Fatal("acquire 没有进入轮询")
	}
	if err := syscall.Kill(syscall.Getpid(), syscall.SIGINT); err != nil {
		t.Fatalf("发送 SIGINT 失败: %v", err)
	}

	select {
	case code := <-done:
		if code != 130 {
			t.Fatalf("Ctrl-C 后应退出 130，实际 %d（stderr=%s）", code, errOut.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("收到 SIGINT 后没有退出")
	}

	if deletedPath != "/tasks/req-1?kind=acquire" {
		t.Fatalf("取消请求路径=%q", deletedPath)
	}
	if pid, ok := deletedBody["host_pid"].(float64); !ok || int(pid) != 222 {
		t.Fatalf("取消请求要带上调用方 PID，实际 %v", deletedBody["host_pid"])
	}
	if !strings.Contains(out.String(), "取消") {
		t.Fatalf("输出里没有取消提示: %s", out.String())
	}
}
