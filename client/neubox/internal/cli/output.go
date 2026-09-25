package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

func (a *app) workerFailure(status int, raw []byte) int {
	message, code := api.ErrorDetails(status, raw)
	if a.jsonOutput {
		output := map[string]any{
			"error":       message,
			"http_status": status,
		}
		if code != "" {
			output["code"] = code
		}
		_ = printJSONValue(a.errOut, output)
		return 1
	}
	fmt.Fprintf(a.errOut, "[neubox] 操作失败 (HTTP %d): %s\n", status, message)
	if code != "" {
		fmt.Fprintf(a.errOut, "    code: %s\n", code)
	}
	return 1
}

func printJSON(writer io.Writer, raw []byte) error {
	if len(bytes.TrimSpace(raw)) == 0 {
		fmt.Fprintln(writer, "{}")
		return nil
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		_, _ = writer.Write(raw)
		if len(raw) == 0 || raw[len(raw)-1] != '\n' {
			fmt.Fprintln(writer)
		}
		return err
	}
	return printJSONValue(writer, value)
}

func printJSONValue(writer io.Writer, value any) error {
	encoder := json.NewEncoder(writer)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}
