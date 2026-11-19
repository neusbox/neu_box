package cli

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

const (
	defaultWaitInterval = 2 * time.Second
	waitLogChunkSize    = int64(64 * 1024)
)

type waitOptions struct {
	taskIDs  []string
	interval time.Duration
	timeout  time.Duration
}

type taskLogResponse struct {
	Data      string `json:"data"`
	Offset    int64  `json:"offset"`
	TotalSize int64  `json:"total_size"`
}

func (a *app) runWait(args []string) int {
	options, err := parseWaitOptions(args)
	if err != nil {
		return a.usageError(err.Error())
	}

	if a.jsonOutput || len(options.taskIDs) > 1 {
		return a.waitForTaskSnapshots(options)
	}
	taskID := options.taskIDs[0]
	pathID := url.PathEscape(taskID)
	started := time.Now()
	logOffset := int64(0)
	lastStatus := ""
	for {
		response, code := a.fetchTaskResult(pathID)
		if code != 0 {
			return code
		}
		if response.Status != lastStatus {
			fmt.Fprintf(a.errOut, "[neubox] task %s status: %s\n", taskID, response.Status)
			lastStatus = response.Status
		}

		logOffset, code = a.drainTaskLog(pathID, logOffset)
		if code != 0 {
			return code
		}

		switch response.Status {
		case "completed":
			a.printWaitResult(response)
			return 0
		case "failed":
			a.printWaitResult(response)
			return 1
		case "queued", "running":
			// Keep following the same task.
		default:
			return a.internalError(
				"invalid_worker_response",
				fmt.Errorf("Worker 返回未知任务状态: %q", response.Status),
			)
		}

		if options.timeout > 0 && time.Since(started) >= options.timeout {
			return a.internalError(
				"wait_timeout",
				fmt.Errorf("等待任务 %s 超时；远端任务仍会继续运行", taskID),
			)
		}
		time.Sleep(options.interval)
	}
}

func (a *app) waitForTaskSnapshots(options waitOptions) int {
	ctx := context.Background()
	if options.timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, options.timeout)
		defer cancel()
	}
	var previous string
	for {
		rows, code := a.taskHealthSnapshot(ctx, options.taskIDs)
		if code != 0 {
			return code
		}
		encoded, err := json.Marshal(rows)
		if err != nil {
			return a.internalError("invalid_worker_response", err)
		}
		if string(encoded) != previous {
			if a.jsonOutput {
				fmt.Fprintln(a.out, string(encoded))
			} else {
				a.printTaskHealth(rows, nil)
			}
			previous = string(encoded)
		}
		terminal, success := true, true
		for _, row := range rows {
			switch row.Status {
			case "completed":
				success = success && row.Result != nil && row.Result.ReturnCode != nil && *row.Result.ReturnCode == 0
			case "failed", "cancelled", "canceled":
				success = false
			default:
				terminal = false
			}
		}
		if terminal {
			if success {
				return 0
			}
			return 1
		}
		timer := time.NewTimer(options.interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return a.waitSnapshotTimeout()
		case <-timer.C:
		}
	}
}

func (a *app) waitSnapshotTimeout() int {
	a.printError("wait_timeout", "监控超时，远端任务仍会继续运行")
	return 124
}

func parseWaitOptions(args []string) (waitOptions, error) {
	options := waitOptions{interval: defaultWaitInterval}
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "--interval":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.interval, err = time.ParseDuration(raw)
			if err != nil || options.interval <= 0 {
				return options, fmt.Errorf("--interval 必须是正 duration，例如 2s: %q", raw)
			}
		case "--timeout":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			if raw == "0" {
				options.timeout = 0
				continue
			}
			options.timeout, err = time.ParseDuration(raw)
			if err != nil || options.timeout < 0 {
				return options, fmt.Errorf("--timeout 必须是非负 duration，例如 30m；0 表示不限: %q", raw)
			}
		default:
			argument := strings.TrimSpace(args[index])
			if strings.HasPrefix(argument, "-") {
				return options, fmt.Errorf("未知 wait 选项: %s", argument)
			}
			if argument == "" {
				return options, errors.New("用法: neubox wait <task_id> [<task_id> ...] [--interval 2s] [--timeout 0]")
			}
			options.taskIDs = append(options.taskIDs, argument)
		}
	}
	if len(options.taskIDs) == 0 {
		return options, errors.New("用法: neubox wait <task_id> [<task_id> ...] [--interval 2s] [--timeout 0]")
	}
	return options, nil
}

func (a *app) fetchTaskResult(pathID string) (taskResultResponse, int) {
	status, raw, err := a.worker.Request(http.MethodGet, "/tasks/"+pathID, nil, nil)
	if err != nil {
		return taskResultResponse{}, a.requestError(err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		return taskResultResponse{}, a.workerFailure(status, raw)
	}
	var response taskResultResponse
	if err := api.DecodeJSON(raw, &response); err != nil {
		return taskResultResponse{}, a.internalError("invalid_worker_response", err)
	}
	return response, 0
}

func (a *app) drainTaskLog(pathID string, offset int64) (int64, int) {
	for {
		query := url.Values{
			"limit":  []string{strconv.FormatInt(waitLogChunkSize, 10)},
			"offset": []string{strconv.FormatInt(offset, 10)},
		}
		status, raw, err := a.worker.Request(
			http.MethodGet,
			"/tasks/"+pathID+"/log",
			query,
			nil,
		)
		if err != nil {
			return offset, a.requestError(fmt.Errorf("获取任务日志: %w", err))
		}
		if err := api.ResponseError(status, raw); err != nil {
			return offset, a.workerFailure(status, raw)
		}
		var response taskLogResponse
		if err := api.DecodeJSON(raw, &response); err != nil {
			return offset, a.internalError("invalid_worker_response", err)
		}
		if response.Offset < 0 || response.TotalSize < response.Offset {
			return offset, a.internalError(
				"invalid_worker_response",
				fmt.Errorf(
					"Worker 返回非法日志范围: offset=%d total_size=%d",
					response.Offset,
					response.TotalSize,
				),
			)
		}
		if response.Data != "" {
			if _, err := io.WriteString(a.out, response.Data); err != nil {
				return offset, a.internalError("log_write_failed", fmt.Errorf("输出任务日志: %w", err))
			}
		}

		available := response.TotalSize - response.Offset
		consumed := waitLogChunkSize
		if available < consumed {
			consumed = available
		}
		offset = response.Offset + consumed
		if offset >= response.TotalSize {
			return offset, 0
		}
	}
}

func (a *app) printWaitResult(response taskResultResponse) {
	fmt.Fprintf(a.errOut, "[neubox] task %s finished: %s", response.TaskID, response.Status)
	if response.Result != nil && response.Result.ReturnCode != nil {
		fmt.Fprintf(a.errOut, " rc=%d", *response.Result.ReturnCode)
	}
	fmt.Fprintln(a.errOut)
	if response.Result != nil {
		if response.Result.TimedOut {
			fmt.Fprintln(a.errOut, "    timed_out: true")
		}
		if response.Result.Error != nil {
			fmt.Fprintf(a.errOut, "    error: %v\n", response.Result.Error)
		}
	}
}
