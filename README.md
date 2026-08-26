# Neu Box — 轻量多节点资源管理与沙盒隔离

**本仓库 = worker（核心）+ 聚合**。

Neu Box 在 GPU/NPU 节点上提供：

- **终端沙盒**：把当前 shell（或 Docker 容器终端）迁入 cgroup 沙盒，
  独占指定设备（BPF 拦截 + cgroup 设备白名单），其他进程看不到这些卡
- **命令任务**：异步提交命令任务，按优先级排队（0=普通，1=赶论文），
  自动分配设备、隔离执行、保留日志
- **设备仲裁**：实时感知沙盒外占用（vLLM 等），空闲设备 = 受管 −
  沙盒分配 − 外部占用

## 三仓库结构

| 仓库 | 角色 | 版本 | 说明 |
|---|---|---|---|
| **neu_box**（本仓库） | worker + 聚合 | 0.3.0+ | 节点侧全部逻辑；e2e 测试；用 submodule 钉住配套版本 |
| [neu_box_webui](https://github.com/neusbox/neu_box_webui) | WebUI（原 master） | 0.0.1+ | 节点池、任务转发、实验记录、Web 界面；独立部署 |
| [neu_box_goClient](https://github.com/neusbox/neu_box_goClient) | `neu-sbox` CLI | 0.0.1+ | Go 静态二进制，直连 worker |

三个仓库**代码零依赖**，只通过 HTTP 契约相交：

```
浏览器 ──session──▶ WebUI :25565 ──转发──▶ worker :59075 ──▶ 沙盒/设备
neu-sbox ─────────────────────────────────▶ worker :59075（直连）
```

兼容矩阵由本仓库的 submodule 指针表达：

```
thirds/webui/     → neu_box_webui@<commit>    # 与当前 worker 版本配套验证过的 WebUI
thirds/goClient/  → neu_box_goClient@<commit> # 与当前 worker 版本配套验证过的客户端
```

更新配套版本：

```bash
git -C thirds/webui fetch && git -C thirds/webui checkout v0.0.2
git add thirds/webui && git commit -m "chore: bump webui to v0.0.2"
```

## worker 部署与运行

发布包：`neu-box-<version>-linux-<arch>.tar.gz`（PyInstaller，含安装器）。

```bash
tar -xzf neu-box-0.3.0-linux-arm64.tar.gz && cd neu-box-0.3.0-linux-arm64
sha256sum -c ../neu-box-0.3.0-linux-arm64.tar.gz.sha256
sudo ./neu-box-install install --role worker      # 首次安装
sudo ./neu-box-install upgrade --role worker      # 升级
sudo ./neu-box-install rollback                   # 回滚
```

也可以运行发布包中的交互管理入口，一处完成安装、升级、回滚、服务管理
和日志查看：

```bash
./run.sh                    # 发布目录或源码仓库中打开菜单
neu-box                     # 首次安装后可直接打开菜单
neu-box status              # 也支持非交互子命令
```

详见 [docs/deployment.md](docs/deployment.md)；API 契约见
[docs/worker-api.md](docs/worker-api.md)（含 `api_version` 语义）；
迁移机制见 [docs/database-migrations.md](docs/database-migrations.md)。

## CLI（neu-sbox，来自 goClient 仓库）

```bash
# 沙盒（终端隔离）
neu-sbox acquire --device-num 2        # 当前 shell 独占 2 张卡
neu-sbox release <sandbox_name>        # 释放
neu-sbox check                         # worker 可达性 + API 版本兼容

# 命令任务
neu-sbox acquire --command "python train.py" --device-num 4 --priority 1
neu-sbox tasks                         # 队列
neu-sbox result <task_id>              # 结果/日志
```

完整用法见 [neu_box_goClient README](https://github.com/neusbox/neu_box_goClient)。

## 开发流程

```bash
uv sync
uv run pytest tests/unit          # 单测（worker + 迁移引擎 + 安装器）
uv run deploy/build_release.py    # 构建发布包
# 或使用统一入口：./run.sh test / ./run.sh build

# e2e（需要已部署的 worker + WebUI）
NEU_BOX_TEST_MASTER=http://<master>:25565 NEU_BOX_TEST_PASS='...' \
  uv run pytest tests/test_queue.py
```

## 仓库布局

```
src/neu_box/
├── config.py, logging_config.py, database/   共享框架（webui 仓库有同源副本）
└── worker/
    ├── app.py               Flask 应用 + /healthz (api_version)
    ├── executor/            任务队列、沙盒管理、设备分配、状态上报
    ├── migrations/          0001..0003（版本钉死，只增不改）
    └── resources/           sandbox.sh、BPF、设备状态脚本
deploy/                      构建、安装器（保留）、spec、配置、systemd
run.sh                       交互式安装、升级、回滚与服务管理入口
tests/unit/                  单测
tests/test_queue.py          e2e 集成（依赖部署环境）
thirds/webui/, thirds/goClient/ submodule（配套版本指针）
docs/                        worker API 契约 + 部署 + 迁移手册
```

## 版本规则

- 版本号：`src/neu_box/__init__.py` 的 `__version__`
- `API_VERSION`：worker HTTP API 版本，仅破坏性变更时 +1
- 同一版本号不得重构建复用（安装器按 SHA256SUMS 钉死，拒绝覆盖）
