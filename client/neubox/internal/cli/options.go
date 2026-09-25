package cli

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

type resourceOptions struct {
	deviceIDs    []string
	deviceNum    int
	deviceNumSet bool
	cpu          int
	cpuSet       bool
	memory       int
	memorySet    bool
}

type acquireOptions struct {
	resourceOptions
	pid    int
	pidSet bool
}

type submitOptions struct {
	resourceOptions
	priority      int
	command       string
	image         string
	workdir       string
	containerUser string
	environment   map[string]string
}

func consumeResourceOption(args []string, index *int, options *resourceOptions) (bool, error) {
	argument := args[*index]
	switch argument {
	case "--device":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		deviceID := strings.TrimSpace(raw)
		if deviceID == "" {
			return true, errors.New("--device 不能为空")
		}
		options.deviceIDs = append(options.deviceIDs, deviceID)
	case "--devices":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		count := len(options.deviceIDs)
		for _, item := range strings.Split(raw, ",") {
			if item = strings.TrimSpace(item); item != "" {
				options.deviceIDs = append(options.deviceIDs, item)
			}
		}
		if len(options.deviceIDs) == count {
			return true, errors.New("--devices 不能为空")
		}
	case "--device-num":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		options.deviceNum, err = nonNegativeInteger("--device-num", raw)
		if err != nil {
			return true, err
		}
		options.deviceNumSet = true
	case "--cpu":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		options.cpu, err = nonNegativeInteger("--cpu", raw)
		if err != nil {
			return true, err
		}
		options.cpuSet = true
	case "--mem":
		raw, err := optionValue(args, index)
		if err != nil {
			return true, err
		}
		options.memory, err = nonNegativeInteger("--mem", raw)
		if err != nil {
			return true, err
		}
		options.memorySet = true
	default:
		return false, nil
	}
	if len(options.deviceIDs) > 0 {
		options.deviceNum = 0
	}
	return true, nil
}

func optionValue(args []string, index *int) (string, error) {
	argument := args[*index]
	if *index+1 >= len(args) {
		return "", fmt.Errorf("%s 缺少参数", argument)
	}
	*index++
	return args[*index], nil
}

func applyPositionalResources(options *resourceOptions, positionals []string) error {
	var err error
	if len(positionals) > 0 && !options.deviceNumSet && len(options.deviceIDs) == 0 {
		options.deviceNum, err = nonNegativeInteger("device_num", positionals[0])
		if err != nil {
			return err
		}
		options.deviceNumSet = true
	}
	if len(positionals) > 1 && !options.cpuSet {
		options.cpu, err = nonNegativeInteger("cpu", positionals[1])
		if err != nil {
			return err
		}
	}
	if len(positionals) > 2 && !options.memorySet {
		options.memory, err = nonNegativeInteger("mem", positionals[2])
		if err != nil {
			return err
		}
	}
	if len(options.deviceIDs) > 0 {
		options.deviceNum = 0
	}
	return nil
}

func validateResourceOptions(options *resourceOptions) error {
	if options.deviceNumSet && len(options.deviceIDs) > 0 {
		return errors.New("--device/--devices 与 --device-num 互斥")
	}
	if !options.deviceNumSet && len(options.deviceIDs) == 0 {
		options.deviceNum = 1
	}
	return nil
}

func nonNegativeInteger(name, raw string) (int, error) {
	value, err := strconv.Atoi(raw)
	if err != nil || value < 0 {
		return 0, fmt.Errorf("%s 必须是非负整数: %q", name, raw)
	}
	return value, nil
}

func positiveInteger(name, raw string) (int, error) {
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s 必须是正整数: %q", name, raw)
	}
	return value, nil
}

func nullableString(value string) *string {
	if value == "" {
		return nil
	}
	copy := value
	return &copy
}
