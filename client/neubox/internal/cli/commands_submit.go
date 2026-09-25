package cli

import (
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/neusbox/neu_box/client/neubox/internal/api"
)

type commandTarget struct {
	Type    string            `json:"type"`
	Image   string            `json:"image"`
	Workdir *string           `json:"workdir"`
	User    *string           `json:"user"`
	Env     map[string]string `json:"env"`
}

type commandRequest struct {
	UserID    string         `json:"user_id"`
	Command   string         `json:"command"`
	DeviceNum int            `json:"device_num"`
	DeviceIDs []string       `json:"device_ids"`
	CPU       int            `json:"cpu"`
	Memory    int            `json:"memory"`
	MemUnit   string         `json:"mem_unit"`
	Priority  int            `json:"priority"`
	Target    *commandTarget `json:"target,omitempty"`
}

type commandResponse struct {
	TaskID   string `json:"task_id"`
	Position int    `json:"position"`
	Priority int    `json:"priority"`
	Error    string `json:"error"`
}

func (a *app) runSubmit(args []string) int {
	options, err := parseSubmitOptions(args)
	if err != nil {
		return a.usageError(err.Error())
	}
	return a.submitCommand(options)
}

func parseSubmitOptions(args []string) (submitOptions, error) {
	options := submitOptions{environment: make(map[string]string)}

	for index := 0; index < len(args); index++ {
		argument := args[index]
		if argument == "--" {
			if options.command != "" {
				return options, errors.New("submit 只能指定一个命令")
			}
			if index+1 >= len(args) {
				return options, errors.New("submit 缺少命令；请在 -- 后指定命令")
			}
			options.command = joinCommandArguments(args[index+1:])
			break
		}
		if handled, err := consumeResourceOption(args, &index, &options.resourceOptions); handled || err != nil {
			if err != nil {
				return options, err
			}
			continue
		}

		switch argument {
		case "--command":
			if options.command != "" {
				return options, errors.New("submit 只能指定一个命令")
			}
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.command = strings.TrimSpace(raw)
		case "--container":
			return options, errors.New("submit 不支持已有容器；请使用 --image IMAGE 创建一次性容器")
		case "--image":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.image = strings.TrimSpace(raw)
			if options.image == "" {
				return options, errors.New("--image 不能为空")
			}
		case "--priority":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.priority, err = nonNegativeInteger("--priority", raw)
			if err != nil {
				return options, err
			}
		case "--workdir":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.workdir = raw
		case "--container-user":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			options.containerUser = raw
		case "--env":
			raw, err := optionValue(args, &index)
			if err != nil {
				return options, err
			}
			key, envValue, found := strings.Cut(raw, "=")
			if !found || strings.TrimSpace(key) == "" {
				return options, fmt.Errorf("--env 必须是 KEY=VALUE: %s", raw)
			}
			options.environment[key] = envValue
		default:
			if strings.HasPrefix(argument, "-") {
				return options, fmt.Errorf("未知 submit 选项: %s", argument)
			}
			return options, fmt.Errorf("无法识别 submit 参数 %q；命令必须放在 -- 后", argument)
		}
	}

	if strings.TrimSpace(options.command) == "" {
		return options, errors.New("submit 缺少命令；请在 -- 后指定命令")
	}
	if options.image == "" && (options.workdir != "" || options.containerUser != "" || len(options.environment) > 0) {
		return options, errors.New("--workdir/--container-user/--env 必须配合 --image")
	}
	if err := validateResourceOptions(&options.resourceOptions); err != nil {
		return options, err
	}
	return options, nil
}

func joinCommandArguments(arguments []string) string {
	if len(arguments) == 1 {
		return arguments[0]
	}
	quoted := make([]string, 0, len(arguments))
	for _, argument := range arguments {
		quoted = append(quoted, quoteCommandArgument(argument))
	}
	return strings.Join(quoted, " ")
}

func quoteCommandArgument(argument string) string {
	if argument == "" {
		return "''"
	}
	if strings.IndexFunc(argument, func(character rune) bool {
		return !(character >= 'a' && character <= 'z') &&
			!(character >= 'A' && character <= 'Z') &&
			!(character >= '0' && character <= '9') &&
			!strings.ContainsRune("_@%+=:,./-", character)
	}) == -1 {
		return argument
	}
	return "'" + strings.ReplaceAll(argument, "'", "'\"'\"'") + "'"
}

func (a *app) submitCommand(options submitOptions) int {
	payload := commandRequest{
		UserID:    a.config.username,
		Command:   options.command,
		DeviceNum: options.deviceNum,
		DeviceIDs: options.deviceIDs,
		CPU:       options.cpu,
		Memory:    options.memory,
		MemUnit:   "GB",
		Priority:  options.priority,
	}
	if options.image != "" {
		payload.Target = &commandTarget{
			Type:    "docker",
			Image:   options.image,
			Workdir: nullableString(options.workdir),
			User:    nullableString(options.containerUser),
			Env:     options.environment,
		}
	}
	status, raw, err := a.worker.Request(http.MethodPost, "/tasks", nil, payload)
	if err != nil {
		return a.requestError(err)
	}
	if err := api.ResponseError(status, raw); err != nil {
		return a.workerFailure(status, raw)
	}
	var response commandResponse
	if err := api.DecodeJSON(raw, &response); err != nil || response.TaskID == "" {
		if err == nil {
			err = errors.New("Worker 响应缺少 task_id")
		}
		return a.internalError("invalid_worker_response", err)
	}
	if a.jsonOutput {
		_ = printJSON(a.out, raw)
		return 0
	}
	fmt.Fprintln(a.out, "[neubox] 任务已提交")
	fmt.Fprintf(a.out, "    ID=%s\n", response.TaskID)
	fmt.Fprintf(a.out, "    queue_position: #%d\n", response.Position)
	if response.Priority > 0 {
		fmt.Fprintf(a.out, "    priority: %d\n", response.Priority)
	}
	fmt.Fprintf(a.out, "    command: %s\n", options.command)
	fmt.Fprintf(a.out, "    follow: neubox wait %s\n", response.TaskID)
	return 0
}
