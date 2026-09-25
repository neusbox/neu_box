package cli

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAcquireRejectsContainerWithoutRequest(t *testing.T) {
	for _, args := range [][]string{{"acquire"}, {"acquire", "--pid", "123"}, {"acquire", "--container", "name"}} {
		calls := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(500) }))
		application, _, _ := testApplication(server.URL)
		application.insideContainer = func() bool { return true }
		if rc := application.run(args); rc != 2 {
			t.Fatalf("rc=%d", rc)
		}
		server.Close()
		if calls != 0 {
			t.Fatal("sent container PID to worker")
		}
	}
}

func TestAcquireWaitsForQueuedResult(t *testing.T) {
	polls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var payload map[string]any
			decodeRequest(t, r, &payload)
			if payload["pid"] != float64(111) {
				t.Errorf("payload=%v", payload)
			}
			if _, ok := payload["container"]; ok {
				t.Error("unexpected container")
			}
			writeJSON(t, w, 202, map[string]any{"acquire_id": "id1", "status": "queued"})
			return
		}
		if r.URL.Path != "/sandbox/acquire/id1" {
			t.Errorf("path=%s", r.URL.Path)
		}
		polls++
		if polls == 1 {
			writeJSON(t, w, 202, map[string]any{"status": "queued"})
			return
		}
		writeJSON(t, w, 201, map[string]any{"sandbox_name": "sbx_yuxd_1.slice", "devices": []string{"1"}})
	}))
	defer server.Close()
	application, out, errOut := testApplication(server.URL)
	if rc := application.run([]string{"acquire"}); rc != 0 {
		t.Fatalf("rc=%d %s", rc, errOut.String())
	}
	if polls != 2 || !strings.Contains(out.String(), "sbx_yuxd_1.slice") {
		t.Fatalf("polls=%d out=%s", polls, out.String())
	}
}

func TestAcquireDefaultsToOneDevice(t *testing.T) {
	var received terminalAcquireRequest
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/sandbox/acquire" {
			t.Errorf("unexpected request: %s %s", request.Method, request.URL.Path)
		}
		decodeRequest(t, request, &received)
		writeJSON(t, writer, http.StatusCreated, map[string]any{
			"sandbox_name": "sbx_yuxd_43210.slice",
			"devices":      []string{"235:1"},
		})
	}))
	defer server.Close()

	application, _, errOut := testApplication(server.URL)
	code := application.run([]string{"acquire"})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if received.DeviceNum != 1 {
		t.Fatalf("expected default device_num=1, got %+v", received)
	}
}

func TestAcquirePositionalZeroDeviceNumIsKept(t *testing.T) {
	var received terminalAcquireRequest
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/sandbox/acquire" {
			t.Errorf("unexpected request: %s %s", request.Method, request.URL.Path)
		}
		decodeRequest(t, request, &received)
		writeJSON(t, writer, http.StatusCreated, map[string]any{
			"sandbox_name": "sbx_yuxd_43210.slice",
			"devices":      []string{},
		})
	}))
	defer server.Close()

	application, _, errOut := testApplication(server.URL)
	code := application.run([]string{"acquire", "0", "2", "4"})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if received.DeviceNum != 0 || received.CPU != 2 || received.Memory != 4 {
		t.Fatalf("positional resource args must be kept, got %+v", received)
	}
}

func TestAcquireRejectsCommandMode(t *testing.T) {
	application, _, errOut := testApplication("http://127.0.0.1:1")
	code := application.run([]string{"acquire", "--command", "echo ok"})
	if code != 2 {
		t.Fatalf("exit=%d", code)
	}
	if !strings.Contains(errOut.String(), "已移至 submit") {
		t.Fatalf("unexpected stderr: %s", errOut.String())
	}
}

func TestAcquireRejectsUnknownOptionBeforeHTTP(t *testing.T) {
	application, _, errOut := testApplication("http://127.0.0.1:1")
	code := application.run([]string{"acquire", "--devcie-num", "1"})
	if code != 2 {
		t.Fatalf("exit=%d", code)
	}
	if !strings.Contains(errOut.String(), "未知 acquire 选项") {
		t.Fatalf("unexpected stderr: %s", errOut.String())
	}
}

func TestAcquirePrintsDockerHint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writeJSON(t, writer, http.StatusCreated, map[string]any{
			"sandbox_name": "sbx_yuxd_42.slice",
			"devices":      []string{"235:1"},
		})
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL)
	if code := application.run([]string{"acquire"}); code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if !strings.Contains(out.String(), "neubox docker run") {
		t.Fatalf("acquire 输出应提示 docker run 用法: %s", out.String())
	}
}
