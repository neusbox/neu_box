package cli

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCheckCompatibleWorker(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		writeJSON(t, w, http.StatusOK, map[string]any{
			"status": "ok", "role": "worker", "api_version": 2,
			"version": "0.4.0", "schema_version": 3,
		})
	}))
	defer server.Close()
	app, out, errOut := testApplication(server.URL)

	if code := app.runCheck(nil); code != 0 {
		t.Fatalf("runCheck code = %d, out=%q err=%q", code, out.String(), errOut.String())
	}
	if !strings.Contains(out.String(), "api_version: 2") || !strings.Contains(out.String(), "兼容") {
		t.Errorf("output missing compatibility line: %q", out.String())
	}
}

func TestCheckOldWorkerWithoutAPIVersion(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusOK, map[string]any{
			"status": "ok", "role": "worker",
			"version": "0.2.1", "schema_version": 2,
		})
	}))
	defer server.Close()
	app, _, errOut := testApplication(server.URL)

	if code := app.runCheck(nil); code != 1 {
		t.Fatalf("runCheck code = %d (旧 worker 应拒绝), err=%q", code, errOut.String())
	}
	if !strings.Contains(errOut.String(), "error") {
		t.Errorf("缺少旧版本 error: %q", errOut.String())
	}
}

func TestCheckIncompatibleWorkerAPI(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusOK, map[string]any{
			"status": "ok", "role": "worker", "api_version": 0,
			"version": "0.3.0", "schema_version": 3,
		})
	}))
	defer server.Close()
	app, _, errOut := testApplication(server.URL)

	if code := app.runCheck(nil); code != 1 {
		t.Fatalf("runCheck code = %d, 期望 1, err=%q", code, errOut.String())
	}
}
