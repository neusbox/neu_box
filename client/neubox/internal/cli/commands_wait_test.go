package cli

import (
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestWaitStreamsNewLogSegmentsUntilCompleted(t *testing.T) {
	resultCalls := 0
	logOffsets := make([]string, 0, 2)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/tasks/abc123":
			resultCalls++
			status := "running"
			var result any
			if resultCalls >= 2 {
				status = "completed"
				result = map[string]any{"returncode": 0, "timed_out": false}
			}
			writeJSON(t, writer, http.StatusOK, map[string]any{
				"task_id": "abc123",
				"status":  status,
				"result":  result,
			})
		case "/tasks/abc123/log":
			offset := request.URL.Query().Get("offset")
			logOffsets = append(logOffsets, offset)
			if offset == "0" {
				writeJSON(t, writer, http.StatusOK, map[string]any{
					"data": "one\n", "offset": 0, "total_size": 4,
				})
				return
			}
			writeJSON(t, writer, http.StatusOK, map[string]any{
				"data": "two\n", "offset": 4, "total_size": 8,
			})
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL)
	code := application.run([]string{"wait", "abc123", "--interval", "1ms"})
	if code != 0 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if out.String() != "one\ntwo\n" {
		t.Fatalf("unexpected streamed log: %q", out.String())
	}
	if !reflect.DeepEqual(logOffsets, []string{"0", "4"}) {
		t.Fatalf("unexpected offsets: %#v", logOffsets)
	}
	for _, expected := range []string{"status: running", "status: completed", "finished: completed rc=0"} {
		if !strings.Contains(errOut.String(), expected) {
			t.Fatalf("missing %q in %s", expected, errOut.String())
		}
	}
}

func TestWaitReturnsFailureForFailedTask(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if strings.HasSuffix(request.URL.Path, "/log") {
			writeJSON(t, writer, http.StatusOK, map[string]any{
				"data": "boom\n", "offset": 0, "total_size": 5,
			})
			return
		}
		writeJSON(t, writer, http.StatusOK, map[string]any{
			"task_id": "failed1",
			"status":  "failed",
			"result": map[string]any{
				"returncode": 2,
				"timed_out":  false,
				"error":      "command failed",
			},
		})
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL)
	code := application.run([]string{"wait", "failed1", "--interval", "1ms"})
	if code != 1 {
		t.Fatalf("exit=%d stderr=%s", code, errOut.String())
	}
	if out.String() != "boom\n" || !strings.Contains(errOut.String(), "finished: failed rc=2") {
		t.Fatalf("stdout=%q stderr=%q", out.String(), errOut.String())
	}
}
