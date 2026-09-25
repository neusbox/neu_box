//go:build integration

package cli

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestIntegrationAcquireAndStatus exercises the public CLI against a real HTTP
// server without Docker, root privileges, or a running Worker.
func TestIntegrationAcquireAndStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/sandbox/acquire":
			writeJSON(t, w, http.StatusCreated, map[string]any{
				"sandbox_name": "sbx_yuxd_42.slice", "devices": []string{"davinci0"},
			})
		case "/sandbox/status":
			writeJSON(t, w, http.StatusOK, map[string]any{
				"sandbox_name": "sbx_yuxd_42.slice", "devices": []string{"davinci0"},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	application, out, errOut := testApplication(server.URL)
	if code := application.run([]string{"acquire", "--device", "0"}); code != 0 {
		t.Fatalf("acquire exit=%d stderr=%s", code, errOut.String())
	}
	if code := application.run([]string{"status"}); code != 0 {
		t.Fatalf("status exit=%d stderr=%s", code, errOut.String())
	}
	if out.Len() == 0 {
		t.Fatal("CLI produced no output")
	}
}
