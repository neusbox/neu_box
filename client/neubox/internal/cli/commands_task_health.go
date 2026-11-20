package cli

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

type taskHealth struct {
	TaskID  string      `json:"task_id"`
	Status  string      `json:"status"`
	CPU     int         `json:"cpu"`
	Mem     string      `json:"mem"`
	Devices []string    `json:"devices"`
	Result  *taskResult `json:"result"`
	LogTail string      `json:"log_tail"`
}

func (a *app) runTaskHealth(ids []string, options tasksOptions) int {
	rows, code := a.taskHealthSnapshot(context.Background(), ids)
	if code == 0 {
		if a.jsonOutput {
			_ = json.NewEncoder(a.out).Encode(rows)
		} else {
			a.printTaskHealth(rows, options.color)
		}
	}
	return code
}

func (a *app) taskHealthSnapshot(ctx context.Context, ids []string) ([]taskHealth, int) {
	rows := make([]taskHealth, 0, len(ids))
	for _, id := range ids {
		path := "/tasks/" + url.PathEscape(id)
		raw, code := a.getTaskHealthData(ctx, path, nil)
		if code != 0 {
			return nil, code
		}
		var row taskHealth
		if err := api.DecodeJSON(raw, &row); err != nil {
			return nil, a.internalError("invalid_worker_response", err)
		}
		log, code := a.getTaskHealthData(ctx, path+"/log", url.Values{"raw": {"1"}, "tail": {"1500"}})
		if code != 0 {
			return nil, code
		}
		row.LogTail = string(log)
		rows = append(rows, row)
	}
	return rows, 0
}

func (a *app) getTaskHealthData(ctx context.Context, path string, query url.Values) ([]byte, int) {
	status, raw, err := a.worker.Get(ctx, path, query)
	if ctx.Err() == context.DeadlineExceeded {
		return nil, a.waitSnapshotTimeout()
	}
	if err != nil {
		return nil, a.requestError(err)
	}
	// Raw log output can contain arbitrary text, including JSON error objects.
	if status < 200 || status >= 300 || (query.Get("raw") != "1" && api.ResponseError(status, raw) != nil) {
		return nil, a.workerFailure(status, raw)
	}
	return raw, 0
}

func (a *app) printTaskHealth(rows []taskHealth, color *bool) {
	for _, row := range rows {
		id := a.taskColor(color, "1;36", row.TaskID)
		status := a.taskColor(color, taskStatusColors[row.Status], row.Status)
		fmt.Fprintf(a.out, "%s %s CPU=%d RAM=%s 设备=%v", id, status, row.CPU, row.Mem, row.Devices)
		if row.Result != nil {
			if row.Result.ReturnCode != nil {
				fmt.Fprintf(a.out, " rc=%d", *row.Result.ReturnCode)
			}
			if row.Result.TimedOut {
				fmt.Fprint(a.out, " timed_out=true")
			}
			if row.Result.Error != nil {
				fmt.Fprintf(a.out, " error=%v", row.Result.Error)
			}
		}
		fmt.Fprintln(a.out)
		if row.LogTail != "" {
			fmt.Fprintln(a.out, strings.TrimRight(row.LogTail, "\r\n"))
		}
	}
}
