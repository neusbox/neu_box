package api

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

type Client struct {
	baseURL string
	HTTP    *http.Client
}

type apiErrorBody struct {
	Error string `json:"error"`
	Code  string `json:"code"`
}

func NewClient(baseURL string) *Client {
	return &Client{
		baseURL: strings.TrimRight(strings.TrimSpace(baseURL), "/"),
		HTTP:    defaultHTTPClient(),
	}
}

func (client *Client) Request(
	method string,
	path string,
	query url.Values,
	payload any,
) (int, []byte, error) {
	return client.requestWithHeaders(method, path, query, "", payload)
}

func (client *Client) RequestAuth(
	method string,
	path string,
	query url.Values,
	token string,
	payload any,
) (int, []byte, error) {
	return client.requestWithHeaders(method, path, query, token, payload)
}

func (client *Client) requestWithHeaders(
	method string,
	path string,
	query url.Values,
	token string,
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
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := client.HTTP.Do(request)
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

func ResponseError(status int, raw []byte) error {
	message, code := ErrorDetails(status, raw)
	if status >= 200 && status < 300 && message == "" {
		return nil
	}
	if code != "" {
		return fmt.Errorf("Worker 返回 HTTP %d: %s (%s)", status, message, code)
	}
	return fmt.Errorf("Worker 返回 HTTP %d: %s", status, message)
}

func ErrorDetails(status int, raw []byte) (string, string) {
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

func DecodeJSON(raw []byte, destination any) error {
	if err := json.Unmarshal(raw, destination); err != nil {
		return fmt.Errorf("解析 Worker JSON: %w", err)
	}
	return nil
}

func defaultHTTPClient() *http.Client {
	return &http.Client{
		Transport: &http.Transport{Proxy: nil},
		Timeout:   requestTimeout,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}
