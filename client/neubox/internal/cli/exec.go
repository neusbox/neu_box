package cli

import (
	"errors"
	"os"
	"os/exec"
	"syscall"
)

// ExecFn 用 argv 替换当前进程映像。
//
// 生产实现是 syscall.Exec —— 成功后不返回。只有真的替换进程，docker 才能拿到
// 控制终端、信号和退出码（`-it` 要的就是这个），所以这里不能改成起子进程。
// 注入是为了让测试不必真的去调 docker。
//
// **argv 是完整的 execve 参数表**：argv[0] 必须是可执行文件名本身，调用方要
// 自己放进去（`syscall.Exec` 不做这件事）。少了它，被执行程序的 os.Args[0]
// 会变成第一个真实参数 —— docker 就会把 `--annotation` 当顶层 flag 拒掉。
//
// 替换后 stdin/stdout/stderr 沿用的是当前进程的 0/1/2 号 fd；生产入口
// Run(os.Stdout, os.Stderr) 给的就是它们。
type ExecFn func(path string, argv []string, env []string) error

// LookPathFn 解析可执行文件路径；注入是为了让测试不依赖 PATH 上装没装 docker。
type LookPathFn func(file string) (string, error)

// OutputFn 跑一条命令并拿回它的 stdout（`neubox docker start` 用来查容器 ID）。
// stderr 直接透给用户，docker 自己的报错不该被我们吞掉。
type OutputFn func(path string, args ...string) ([]byte, error)

// RunFn 起一条子进程，把它接到当前进程的 0/1/2 号 fd 上，返回退出码。
//
// `docker run` 用 ExecFn 把自己换成 docker；`docker start` 不行 —— start 之后
// 还要回查借条有没有被认领，进程不能被替换掉。
type RunFn func(path string, argv []string, env []string) (int, error)

func defaultExec(path string, argv []string, env []string) error {
	return syscall.Exec(path, argv, env)
}

func defaultLookPath(file string) (string, error) {
	return exec.LookPath(file)
}

func defaultOutput(path string, args ...string) ([]byte, error) {
	command := exec.Command(path, args...)
	command.Stderr = os.Stderr
	return command.Output()
}

func defaultRun(path string, argv []string, env []string) (int, error) {
	command := exec.Command(path, argv[1:]...)
	command.Args = argv
	command.Env = env
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	err := command.Run()
	if err == nil {
		return 0, nil
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		// 退出码由 docker 决定，交给调用方原样返回。
		return exitError.ExitCode(), nil
	}
	return -1, err
}
