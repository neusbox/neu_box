# Tests

`tests/unit/` 是不接触生产服务的默认测试；`tests/test_*.py` 是针对已部署 Master/Worker 的真实 HTTP 与硬件集成测试。

## 单元与发布流程测试

```bash
UV_CACHE_DIR=/tmp/neu-box-uv-cache uv run --frozen pytest -q
```

它覆盖数据库迁移、应用工厂、发布包校验，以及无特权目录中的安装、升级、失败恢复和数据库回滚。`pyproject.toml` 将 pytest 默认范围限制在 `tests/unit/`，避免开发时意外向生产节点提交任务。

## 已部署环境集成测试

这些脚本会访问真实节点、创建实验记录或提交任务，必须在维护窗口中明确执行：

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
| `test_nodes.py` | 所有节点在线、单节点 `/status` 字段完整、`/config` 列表可读 |
| `test_queue.py` | GPU 并发排队（不超过可用数）、FIFO 顺序、批量删除 |
| `test_command.py` | 必填字段校验、`stdout`/`stderr` 内容验证、多种资源配置 |
| `test_experiments.py` | 实验 CRUD、按标题/标签/创建者搜索、空实验 |

## 要求

- 测试机器能访问 Master
- 如果走代理连不上，先执行 `proxy_off` 关掉代理
- Worker 节点上存在 `pengyt` 和 `lipz` 两个系统用户（用于提交任务）
- 不会修改 `config.json` 中的节点配置（只读）
