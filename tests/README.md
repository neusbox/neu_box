# Tests

`tests/unit/` 是不接触生产服务的默认测试；`tests/test_nodes.py`、
`tests/test_queue.py`、`tests/test_command.py` 和 `tests/test_experiments.py` 是针对
已部署 Master/Worker 的真实 HTTP 与硬件集成测试。`tests/test_deployment.sh` 则
直接验收单个已部署 Worker。

## 单元与 RPM 流程测试

```bash
./run.sh test

# 直接向 pytest 传参
./run.sh test -k rpm
```

单元测试覆盖数据库迁移、应用工厂、RPM 标准路径、spec scriptlet 边界、管理 CLI
参数转发和 RPM source staging。测试使用伪造 payload，不安装 RPM、不调用生产
systemd，也不访问已部署 Worker。`pyproject.toml` 将 pytest 默认范围限制在
`tests/unit/`，避免开发时意外向生产节点提交任务。

真实构建当前架构 RPM 使用：

```bash
./run.sh build
# → dist/rpm/neu-box-worker-<version>-<release>.<arch>.rpm
```

源码 `run.sh` 只提供 `build`、`test`、`deployment-test`；安装后的服务与数据库管理
使用 `/usr/sbin/neu-box`。

## 已部署环境集成测试

这些脚本会访问真实节点、创建实验记录或提交任务，必须在维护窗口中明确执行：

### Worker 实机一键验收

在 Worker 本机执行下面的脚本，可一次验证健康检查、v2 路由、系统用户校验、
正常任务、Shell 解析错误、单卡分配/释放，以及 Reaper 对父子进程和设备的最终回收：

```bash
# 默认地址 http://127.0.0.1:59075，默认使用当前系统用户
./tests/test_deployment.sh

# 指定 Worker 和任务用户
./tests/test_deployment.sh --url http://10.0.0.8:59075 --user pengyt

# 验收非当前源码版本时必须显式指定，默认要求与仓库版本完全一致
./tests/test_deployment.sh --expected-version 0.5.0

# 没有设备或只验证 HTTP/任务链路
./tests/test_deployment.sh --skip-device

# 临时不等待 Reaper 周期
./tests/test_deployment.sh --skip-reaper
```

脚本会提交真实任务并短暂独占一张空闲设备卡，适合在维护窗口执行。本机、当前用户、
脚本在创建任何任务前先要求 `/healthz` 版本与仓库版本完全一致，避免拿旧 Worker
误验新实现；只有明确验收其他构建时才使用 `--expected-version` 覆盖。
至少两张受管卡的条件满足时，它还会直接打开真实 `/dev` 字符设备，验证宿主进程不能
访问已预留卡、sandbox 内只能访问获配卡，并用宿主可打开未分配卡作为驱动/权限对照。
退出时会清理本次创建的任务、测试进程和沙盒。远程地址仍可验证 API 与任务链路，但依赖 Worker
宿主机 PID/cgroup 的 Reaper 用例会自动跳过；要验证“父进程退出但子进程仍使用卡，
子进程退出后卡最终收回”，必须在 Worker 本机运行。

### Master/Worker 集成脚本

```bash
# 逐个文件手动运行
python3 tests/test_nodes.py
python3 tests/test_queue.py
python3 tests/test_command.py
python3 tests/test_experiments.py

# 快速模式（缩短 sleep 等待时间）
python3 tests/test_queue.py --quick

# 只跑匹配的测试
python3 tests/test_queue.py --test=并发
python3 tests/test_command.py --test=提交

# 指定 Master 地址（默认 http://219.216.64.157:25565）
NEU_BOX_TEST_MASTER=http://127.0.0.1:25565 python3 tests/test_nodes.py

# Master 开启登录鉴权时指定登录用户和密码（用户默认 admin，密码默认空）
NEU_BOX_TEST_USER=admin NEU_BOX_TEST_PASS=secret \
  python3 tests/test_nodes.py
```

这里的 `NEU_BOX_TEST_USER` 是 Master 登录用户；单独运行
`test_deployment.sh` 时，同名变量表示 Worker 上执行测试任务的 Linux 用户。

## 文件说明

| 文件 | 内容 |
|---|---|
| `common.py` | Master 地址、按需登录、HTTP 请求、断言和 `run_tests()` 框架 |
| `run_tests.sh` | 依次运行四个真实 Master/Worker 集成脚本 |
| `test_deployment.sh` | 已部署 Worker 一键实机验收；包含 API、任务错误、真实设备节点 BPF 隔离、设备释放和 Reaper 最终回收 |
| `test_nodes.py` | 所有节点在线、单节点 `/nodes/<id>/status` 字段完整、`/nodes/config` 列表可读 |
| `test_queue.py` | 设备并发排队、优先级调度、同优先级 FIFO 和非法优先级校验；另保留默认未启用的批量删除用例 |
| `test_command.py` | Master 任务字段校验、结果内容和多种资源配置 |
| `test_experiments.py` | 实验 CRUD、按标题/标签/创建者搜索、空实验 |

## 要求

- 测试机器能访问 Master
- Worker 一键验收需要 `bash`、`curl` 和 `python3`；完整 Reaper 用例必须在 Worker 本机运行
- 如果走代理连不上，先执行 `proxy_off` 关掉代理
- Master 集成脚本提交的 Worker 节点上存在 `pengyt` 和 `lipz` 两个系统用户
- 不会修改 `config.json` 中的节点配置（只读）
