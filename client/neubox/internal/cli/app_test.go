package cli

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Worker 的错误响应要映射成非 0 退出码与可读的 stderr 摘要。
func TestWorkerErrorReturnsNonZero(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writeJSON(t, writer, http.StatusConflict, map[string]any{
			"error": "already allocated",
			"code":  "docker_container_pid_changed",
		})
	}))
	defer server.Close()

	application, _, errOut := testApplication(server.URL)
	code := application.run([]string{"acquire", "--device-num", "1"})
	if code != 1 {
		t.Fatalf("exit=%d", code)
	}
	if !strings.Contains(errOut.String(), "HTTP 409") {
		t.Fatalf("unexpected stderr: %s", errOut.String())
	}
}

func TestHelpUsesReadableIndentation(t *testing.T) {
	application, out, errOut := testApplication("http://127.0.0.1:1")
	code := application.run([]string{"--help"})
	if code != 0 || errOut.Len() != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	for _, expected := range []string{
		"\n    neubox acquire",
		"\n    --device ID",
		"\n    list ",
		"\n    NEU_BOX_URL",
	} {
		if !strings.Contains(out.String(), expected) {
			t.Fatalf("missing %q in help:\n%s", expected, out.String())
		}
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
