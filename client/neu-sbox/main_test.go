package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func testApplication(serverURL, stateDir string) (*app, *bytes.Buffer, *bytes.Buffer) {
	out := &bytes.Buffer{}
	errOut := &bytes.Buffer{}
	application := &app{
		config: config{
			workerURL: serverURL,
			username:  "yuxd",
			stateDir:  stateDir,
		},
		out:             out,
		errOut:          errOut,
		api:             newAPIClient(serverURL),
		getPID:          func() int { return 222 },
		getPPID:         func() int { return 111 },
		insideContainer: func() bool { return false },
		readFile:        os.ReadFile,
	}
	return application, out, errOut
}

func writeJSON(t *testing.T, writer http.ResponseWriter, status int, value any) {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Errorf("marshal response: %v", err)
		http.Error(writer, err.Error(), http.StatusInternalServerError)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	if _, err := writer.Write(raw); err != nil {
		t.Errorf("write response: %v", err)
	}
}

func decodeRequest(t *testing.T, request *http.Request, destination any) {
	t.Helper()
	defer request.Body.Close()
	if err := json.NewDecoder(request.Body).Decode(destination); err != nil {
		t.Errorf("decode request: %v", err)
	}
}

func TestTerminalAcquireUsesParentPIDAndRemembersContainer(t *testing.T) {
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

	stateDir := t.TempDir()
	application, out, errOut := testApplication(server.URL, stateDir)
	application.insideContainer = func() bool { return true }
	code := application.run([]string{
		"acquire", "--container", "oprace", "--device-num", "1",
	})

	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if received.PID != 111 || received.Container != "oprace" || received.DeviceNum != 1 {
		t.Fatalf("unexpected payload: %+v", received)
	}
	state, err := os.ReadFile(filepath.Join(stateDir, "sbx_yuxd_43210.slice.container"))
	if err != nil {
		t.Fatal(err)
	}
	if string(state) != "oprace\n" {
		t.Fatalf("unexpected state: %q", state)
	}
	if !strings.Contains(out.String(), "沙盒已创建") {
		t.Fatalf("unexpected output: %s", out.String())
	}
}

func TestReleaseUsesClientPIDAndSavedContainer(t *testing.T) {
	var received map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/sandbox/release" {
			t.Errorf("unexpected request: %s %s", request.Method, request.URL.Path)
		}
		decodeRequest(t, request, &received)
		writeJSON(t, writer, http.StatusOK, map[string]any{
			"sandbox_name": "sbx_yuxd_43210.slice",
			"message":      "released",
		})
	}))
	defer server.Close()

	stateDir := t.TempDir()
	application, _, errOut := testApplication(server.URL, stateDir)
	application.insideContainer = func() bool { return true }
	if err := application.rememberContainer("sbx_yuxd_43210.slice", "oprace"); err != nil {
		t.Fatal(err)
	}
	code := application.run([]string{"release", "sbx_yuxd_43210.slice"})

	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if received["container"] != "oprace" || received["pid"] != float64(111) || received["client_pid"] != float64(222) {
		t.Fatalf("unexpected payload: %#v", received)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "sbx_yuxd_43210.slice.container")); !os.IsNotExist(err) {
		t.Fatalf("state file remains: %v", err)
	}
}

func TestCommandAcquireBuildsDockerTarget(t *testing.T) {
	var received commandRequest
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		decodeRequest(t, request, &received)
		writeJSON(t, writer, http.StatusAccepted, map[string]any{
			"task_id":  "abc123",
			"position": 2,
		})
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL, t.TempDir())
	code := application.run([]string{
		"acquire",
		"--devices", "1,3",
		"--cpu", "4",
		"--mem", "8",
		"--container", "training-01",
		"--workdir", "/workspace",
		"--container-user", "root",
		"--env", "MODE=perf",
		"--command", "python train.py",
	})

	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if received.DeviceNum != 0 || len(received.DeviceIDs) != 2 || received.Target == nil {
		t.Fatalf("unexpected request: %+v", received)
	}
	if received.Target.Container != "training-01" || received.Target.Env["MODE"] != "perf" {
		t.Fatalf("unexpected target: %+v", received.Target)
	}
	if received.Target.Workdir == nil || *received.Target.Workdir != "/workspace" {
		t.Fatalf("unexpected workdir: %+v", received.Target.Workdir)
	}
	if !strings.Contains(out.String(), "ID=abc123") {
		t.Fatalf("unexpected output: %s", out.String())
	}
}

func TestCommandAcquirePriority(t *testing.T) {
	var received commandRequest
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		decodeRequest(t, request, &received)
		writeJSON(t, writer, http.StatusAccepted, map[string]any{
			"task_id":  "abc123",
			"position": 1,
			"priority": 1,
		})
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL, t.TempDir())
	code := application.run([]string{
		"acquire", "--device-num", "1", "--priority", "1", "--command", "echo hi",
	})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if received.Priority != 1 {
		t.Fatalf("unexpected priority: %+v", received)
	}
	if !strings.Contains(out.String(), "优先级: 1") {
		t.Fatalf("missing priority notice: %s", out.String())
	}

	// 不带 --priority 时 priority 应为 0
	received = commandRequest{}
	code = application.run([]string{
		"acquire", "--device-num", "1", "--command", "echo hi",
	})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if received.Priority != 0 {
		t.Fatalf("unexpected priority: %+v", received)
	}

	// 非法 priority（负数）在客户端被拒绝
	code = application.run([]string{
		"acquire", "--device-num", "1", "--priority", "-1", "--command", "echo hi",
	})
	if code == 0 {
		t.Fatalf("negative priority should be rejected: %s", out.String())
	}
}

func TestContainerStatusQueriesParentPID(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Query().Get("container") != "oprace" || request.URL.Query().Get("pid") != "111" {
			t.Errorf("unexpected query: %s", request.URL.RawQuery)
		}
		writeJSON(t, writer, http.StatusOK, map[string]any{
			"sandboxes":       []any{},
			"current_sandbox": "sbx_yuxd_43210.slice",
		})
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL, t.TempDir())
	application.config.container = "oprace"
	code := application.run([]string{"status"})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if !strings.Contains(out.String(), "sbx_yuxd_43210.slice") {
		t.Fatalf("unexpected output: %s", out.String())
	}
}

func TestHostStatusReadsProcWithoutExternalCommands(t *testing.T) {
	application, out, errOut := testApplication("http://127.0.0.1:1", t.TempDir())
	application.readFile = func(path string) ([]byte, error) {
		if path != "/proc/111/cgroup" {
			t.Fatalf("unexpected path: %s", path)
		}
		return []byte("0::/sandbox_sbx_yuxd_43210.slice\n"), nil
	}
	code := application.run([]string{"status"})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if !strings.Contains(out.String(), "sandbox_sbx_yuxd_43210.slice") {
		t.Fatalf("unexpected output: %s", out.String())
	}
}

func TestWorkerErrorReturnsNonZero(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writeJSON(t, writer, http.StatusConflict, map[string]any{
			"error": "already allocated",
			"code":  "docker_container_pid_changed",
		})
	}))
	defer server.Close()

	application, _, errOut := testApplication(server.URL, t.TempDir())
	code := application.run([]string{"acquire", "--device-num", "1"})
	if code != 1 {
		t.Fatalf("exit=%d", code)
	}
	if !strings.Contains(errOut.String(), "HTTP 409") {
		t.Fatalf("unexpected stderr: %s", errOut.String())
	}
}

func TestAcquireRejectsUnknownOptionBeforeHTTP(t *testing.T) {
	application, _, errOut := testApplication("http://127.0.0.1:1", t.TempDir())
	code := application.run([]string{"acquire", "--devcie-num", "1"})
	if code != 2 {
		t.Fatalf("exit=%d", code)
	}
	if !strings.Contains(errOut.String(), "未知 acquire 选项") {
		t.Fatalf("unexpected stderr: %s", errOut.String())
	}
}

func TestHTTPClientDoesNotUseProxyEnvironment(t *testing.T) {
	client := defaultHTTPClient()
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("unexpected transport: %T", client.Transport)
	}
	if transport.Proxy != nil {
		t.Fatal("proxy callback must be nil")
	}
}

func TestPrintJSONDecodesEscapedUnicode(t *testing.T) {
	output := &bytes.Buffer{}
	if err := printJSON(output, []byte(`{"message":"\u5df2\u91ca\u653e"}`)); err != nil {
		t.Fatal(err)
	}
	if output.String() != "{\n  \"message\": \"已释放\"\n}\n" {
		t.Fatalf("unexpected output: %q", output.String())
	}
}

func TestResultPrintsLogAndSummary(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/command/result/abc123":
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
		case "/command/result/abc123/log":
			_, _ = io.WriteString(writer, "hello\n")
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL, t.TempDir())
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
