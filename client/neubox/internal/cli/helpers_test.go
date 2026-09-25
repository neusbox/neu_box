package cli

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"testing"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

func testApplication(serverURL string) (*app, *bytes.Buffer, *bytes.Buffer) {
	out := &bytes.Buffer{}
	errOut := &bytes.Buffer{}
	application := &app{
		config: config{
			workerURL: serverURL,
			username:  "yuxd",
		},
		out:             out,
		errOut:          errOut,
		worker:          api.NewClient(serverURL),
		getPID:          func() int { return 222 },
		getPPID:         func() int { return 111 },
		insideContainer: func() bool { return false },
		readFile:        os.ReadFile,
		// 测试里不碰真的 docker：路径写死，exec 只当"成功替换进程"。
		lookPath: func(string) (string, error) { return "/usr/bin/docker", nil },
		execFn:   func(string, []string, []string) error { return nil },
		// inspect 与 start 也一样：默认是"不该被调到"，用例自己替换。
		outputFn: func(string, ...string) ([]byte, error) {
			return nil, errors.New("测试不该调用 docker inspect")
		},
		runFn: func(string, []string, []string) (int, error) {
			return 0, errors.New("测试不该直接跑 docker start")
		},
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
