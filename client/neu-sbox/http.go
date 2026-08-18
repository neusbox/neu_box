package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const requestTimeout = 30 * time.Second

type apiClient struct {
	baseURL string
	client  *http.Client
}

type apiErrorBody struct {
	Error string `json:"error"`
	Code  string `json:"code"`
}

func newAPIClient(baseURL string) *apiClient {
	return &apiClient{
		baseURL: strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		client:  defaultHTTPClient(),
	}
}

func (client *apiClient) request(
	method string,
	path string,
	query url.Values,
	payload any,
) (int, []byte, error) {
	endpoint := client.baseURL + path
	if len(query) > 0 {
		endpoint += "?" + query.Encode()
	}

	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return 0, nil, fmt.Errorf("编码请求 JSON: %w", err)
		}
		body = bytes.NewReader(encoded)
	}

	request, err := http.NewRequest(method, endpoint, body)
	if err != nil {
		return 0, nil, fmt.Errorf("创建 HTTP 请求: %w", err)
	}
	if payload != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.client.Do(request)
	if err != nil {
		return 0, nil, err
	}
	defer response.Body.Close()

	raw, err := io.ReadAll(response.Body)
	if err != nil {
		return response.StatusCode, nil, fmt.Errorf("读取 Worker 响应: %w", err)
	}
	return response.StatusCode, raw, nil
}

func responseError(status int, raw []byte) error {
	message, code := apiErrorDetails(status, raw)
	if status >= 200 && status < 300 && message == "" {
		return nil
	}
	if code != "" {
		return fmt.Errorf("Worker 返回 HTTP %d: %s (%s)", status, message, code)
	}
	return fmt.Errorf("Worker 返回 HTTP %d: %s", status, message)
}

func apiErrorDetails(status int, raw []byte) (string, string) {
	var body apiErrorBody
	_ = json.Unmarshal(raw, &body)
	if status >= 200 && status < 300 && body.Error == "" {
		return "", ""
	}
	message := strings.TrimSpace(body.Error)
	if message == "" {
		message = strings.TrimSpace(string(raw))
	}
	if message == "" {
		message = http.StatusText(status)
	}
	return message, body.Code
}

func (a *app) workerFailure(status int, raw []byte) int {
	message, code := apiErrorDetails(status, raw)
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
	fmt.Fprintf(a.errOut, "[neu-sbox] 操作失败 (HTTP %d): %s\n", status, message)
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

func decodeJSON(raw []byte, destination any) error {
	if err := json.Unmarshal(raw, destination); err != nil {
		return fmt.Errorf("解析 Worker JSON: %w", err)
	}
	return nil
}
