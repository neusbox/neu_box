# Tests

`tests/unit/` 是不接触生产服务的默认测试；`tests/test_*.py` 是针对已部署 Master/Worker 的真实 HTTP 与硬件集成测试。

## 单元与发布流程测试

```bash
UV_CACHE_DIR=/tmp/neu-box-uv-cache uv run --frozen pytest -q
```

它覆盖数据库迁移、应用工厂、发布包校验，以及无特权目录中的安装、升级、失败恢复和数据库回滚。`pyproject.toml` 将 pytest 默认范围限制在 `tests/unit/`，避免开发时意外向生产节点提交任务。

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

# 没有设备或只验证 HTTP/任务链路
./tests/test_deployment.sh --skip-device

# 临时不等待 Reaper 周期
./tests/test_deployment.sh --skip-reaper
```

脚本会提交真实任务并短暂独占一张空闲设备卡，适合在维护窗口执行。退出时会清理
本次创建的任务、测试进程和沙盒。远程地址仍可验证 API 与任务链路，但依赖 Worker
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

# 指定 Master 地址（默认 http://202.199.13.164:25565）
NEU_BOX_MASTER=http://127.0.0.1:25565 python3 tests/test_nodes.py
```

## 文件说明

| 文件 | 内容 |
|---|---|
| `common.py` | HTTP 请求 (`get`/`post`/`put`/`delete`)、断言 (`assert_ok`/`assert_eq`/`assert_gt`/`assert_in`)、`run_tests()` 框架 |
| `test_deployment.sh` | 已部署 Worker 一键实机验收；包含 API、任务错误、设备分配/释放和 Reaper 最终回收 |
| `test_nodes.py` | 所有节点在线、单节点 `/status` 字段完整、`/config` 列表可读 |
| `test_queue.py` | GPU 并发排队（不超过可用数）、FIFO 顺序、批量删除 |
| `test_command.py` | 必填字段校验、`stdout`/`stderr` 内容验证、多种资源配置 |
| `test_experiments.py` | 实验 CRUD、按标题/标签/创建者搜索、空实验 |

## 要求

- 测试机器能访问 Master
- Worker 一键验收需要 `bash`、`curl` 和 `python3`；完整 Reaper 用例必须在 Worker 本机运行
- 如果走代理连不上，先执行 `proxy_off` 关掉代理
- Worker 节点上存在 `pengyt` 和 `lipz` 两个系统用户（用于提交任务）
- 不会修改 `config.json` 中的节点配置（只读）
