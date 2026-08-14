package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var safeSandboxName = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

func (a *app) stateFile(sandboxName string) (string, error) {
	if !safeSandboxName.MatchString(sandboxName) {
		return "", fmt.Errorf("无效 sandbox 名称: %q", sandboxName)
	}
	return filepath.Join(a.config.stateDir, sandboxName+".container"), nil
}

func (a *app) rememberContainer(sandboxName, container string) error {
	container = strings.TrimSpace(container)
	if container == "" {
		return nil
	}
	path, err := a.stateFile(sandboxName)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(a.config.stateDir, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(a.config.stateDir, 0o700); err != nil {
		return err
	}
	if err := os.WriteFile(path, []byte(container+"\n"), 0o600); err != nil {
		return err
	}
	return os.Chmod(path, 0o600)
}

func (a *app) savedContainer(sandboxName string) (string, error) {
	path, err := a.stateFile(sandboxName)
	if err != nil {
		return "", err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	container := strings.TrimSpace(string(raw))
	if container == "" {
		return "", errors.New("保存的容器标识为空")
	}
	return container, nil
}

func (a *app) forgetContainer(sandboxName string) error {
	path, err := a.stateFile(sandboxName)
	if err != nil {
		return err
	}
	err = os.Remove(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}
