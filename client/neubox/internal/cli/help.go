package cli

import (
	"fmt"
	"io"
)

func (a *app) printHelp() {
	a.printHelpTo(a.out)
}

func (a *app) printHelpTo(writer io.Writer) {
	fmt.Fprint(writer, `neubox — 终端沙盒隔离 / 命令任务提交

用法:
    neubox [--json] acquire [选项...]
    neubox [--json] submit [选项...] -- <command> [args...]
    neubox [--json] release <sandbox_name>
    neubox [--json] cancel <id> [--kind task|acquire]
    neubox [--json] docker run <docker 参数...>
    neubox [--json] docker start <容器> [docker 参数...]
    neubox [--json] {list|status|check|join|result} [参数]
    neubox [--json] tasks [--all] [--since 4h]
    neubox wait <task_id> [--interval 2s] [--timeout 0]

全局选项:
    --json                 只输出机器可读 JSON；失败信息写入 stderr

输出说明:
    默认模式               输出终端可读的结果摘要，不混入 Worker 原始 JSON
    JSON 模式              --json 可放在子命令前或紧跟子命令
                           成功结果写入 stdout，失败对象写入 stderr
    result --json          在任务元数据中增加 log 字段
    wait                   增量输出任务日志并等待终态；暂不支持 --json
    submit ... -- ...      -- 后面的参数属于目标命令，不再解析为 neubox 选项

命令说明:
    acquire                同步申请终端沙盒；返回成功时当前 shell 已进入沙盒
    submit                 异步提交命令任务；返回成功只表示任务已进入队列，
                           需使用 wait <task_id> 跟踪，或用 result 查询快照
    wait <task_id>          增量跟踪日志直到 completed/failed
    result <task_id>        查询异步任务的当前状态、执行结果和完整日志
    docker run              透传 docker run，自动补上沙盒 annotation；
                           没有自己的选项，参数一个不改地交给 docker
    docker start            把停着的容器拉起来，并把当前 shell 的沙盒借给它
                           （容器名放最前面，docker 的选项跟在后面）

acquire 选项:
    --device ID            指定一个卡号，可重复
    --devices 1,3          指定卡号，逗号分隔
    --device-num 2         自动分配卡数量；不指定任何设备选项时默认 1；
                           与 --device/--devices 互斥
    --cpu 4                CPU 核数，0 表示不限
    --mem 8                内存 GB，0 表示不限
    --pid 12345            指定 PID；默认使用启动客户端的当前 shell
    --pid 只能指定宿主 PID；容器内申请会被拒绝

submit 选项:
    --device ID            指定一个卡号，可重复
    --devices 1,3          指定卡号，逗号分隔
    --device-num 2         自动分配卡数量；不指定任何设备选项时默认 1；
                           与 --device/--devices 互斥
    --cpu 4                CPU 核数，0 表示不限
    --mem 8                内存 GB，0 表示不限
    --priority 1           队列优先级，数值越大越先执行；0 表示普通
    --image IMAGE          创建一次性 Docker 目标
    --workdir PATH         Docker 目标的工作目录
    --container-user USER  Docker 目标内的用户
    --env KEY=VALUE        Docker 目标的环境变量，可重复
    --command "..."        命令字符串；也可将命令放在 -- 后

其他命令:
    list                         列出沙盒和资源
    status                       查看当前 shell 所在 sandbox
    check                        检查 Worker 可达性与 API 版本兼容性
    join <sandbox_name>          将 Host 当前 shell 加入已有 sandbox
    tasks                        查看任务队列；默认只显示活跃任务和最近 2h
                                 结束的任务（宁少勿刷屏）
    tasks --all                  显示 worker 返回的全部任务
    tasks --since 4h             自定义时间窗（30m / 6h / 12h 均可）
    result <task_id>             查看任务输出和结果
    wait <task_id>               跟踪增量日志并等待任务结束
    version                      显示客户端版本

示例:
    neubox acquire --device-num 1
    neubox acquire --devices 1,3 --cpu 4 --mem 8
    neubox submit --device 1 -- npu-smi info
    neubox submit --device-num 1 --priority 1 -- python train.py
    neubox submit --device 1 --image training:v1 --workdir /workspace -- python train.py
    neubox wait 7c65d5ac21f4
    neubox tasks --all           # 含较早结束的任务
    neubox release sbx_yuxd_12345.slice
    neubox cancel 7c65d5ac21f4            # 取消排队中/运行中的任务
    neubox cancel 9f0a1b2c3d4e --kind acquire
    neubox docker run --rm -it ubuntu bash
    neubox docker start my-container

环境变量:
    NEU_BOX_URL            Worker 地址，默认 http://127.0.0.1:59075
    NEU_BOX_USER           sandbox/任务用户名

容器必须带 sandbox_cgroup annotation：Worker 靠它把容器登记到沙盒名下，没登记的
容器即使卡空着也一律拿不到设备（fail-closed）。docker run 子命令会自动补上这行
annotation；直接用原生 docker 的话得自己写。客户端是静态二进制，运行时不依赖
Bash、curl 或 Python。

docker start 拿卡靠的不是 annotation（那是建容器时写死的、改不了），而是启动前
先存的一张借条：把当前 shell 所在沙盒借给这个容器，10 秒内有效、一次性。所以
跨 shell 换沙盒继续用同一个容器要走 neubox docker start；直接敲原生
docker start 没有借条，容器起得来但零卡。

cancel 的语义：排队中的条目被摘出队列（任务留痕为 cancelled，记录与日志保留）；
运行中的任务发取消信号；已经拿到卡的 acquire 就地释放，不需要再补一次 release。
acquire 阻塞排队期间按 Ctrl-C 走的就是这条路径（退出码 130）。
`)
}
