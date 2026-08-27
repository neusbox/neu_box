# Worker HTTP API

本文面向不使用 `neu-sbox`、直接接入 Neu Box Worker 的后端系统，适用于
Neu Box `0.3.1`。Worker 默认监听 `http://<worker-host>:59075`，所有接口均
返回 UTF-8；除纯文本日志接口外，请求和响应使用 JSON。

`neu-sbox` 只是这些接口的客户端封装，不是调用 Worker 的必要条件。

接口总览：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/` | 查询服务名称和版本 |
| `GET` | `/healthz` | 健康检查、API 版本和数据库 schema 版本 |
| `GET` | `/status` | 查询 CPU、内存、设备和沙盒状态 |
| `POST` | `/command/run` | 异步提交命令任务 |
| `GET` | `/command/queue` | 查询队列和最近任务 |
| `GET` | `/command/result/<task_id>` | 查询任务状态和退出结果 |
| `GET` | `/command/result/<task_id>/log` | 读取实时任务日志 |
| `POST` | `/command/tasks/delete` | 删除或取消任务 |
| `POST` | `/sandbox/acquire` | 为现有进程立即申请终端沙盒 |
| `POST` | `/sandbox/release` | 释放终端沙盒 |
| `POST` | `/sandbox/join` | 将 Host PID 加入已有沙盒 |
| `GET` | `/sandbox/list` | 查询终端沙盒 |

## 接入前须知

- Worker API 当前没有认证、签名和权限隔离，只能部署在可信内网。
- Worker 服务以 root 运行。`user_id` 是任务的执行身份，不是认证凭据；调用方
  可以指定 Worker 宿主机上任意已存在的用户。
- 直接调用 Worker 时不传 `node_id`。`node_id` 是 WebUI 转发请求时使用的字段，
  不属于 Worker API。
- 命令任务使用 `/command/*`，由 Worker 持久化并排队；终端沙盒使用
  `/sandbox/*`，立即申请资源，不进入任务队列。
- 当前没有 API 版本前缀、幂等键、回调或 Webhook。接入方应记录 `task_id` 并
  轮询结果；不要在响应不确定时盲目重试提交，否则可能产生重复任务。

以下示例统一使用：

```bash
WORKER=http://127.0.0.1:59075
```

如果机器配置了 HTTP 代理，访问内网 Worker 时应绕过代理，例如使用
`curl --noproxy '*'`。

## API 版本

`/healthz` 与 `/status` 均返回 `api_version`（当前 `1`）：

- 仅破坏性变更（删除字段、改变语义）时 +1；新增字段/端点不升版本
- 接入方应对 `api_version < 1` 的连接降级处理或拒绝
- 旧版 worker（< 0.3.0）不上报该字段，接入方应将其视为 best-effort

## 队列接入流程

推荐的最小接入流程是：

```text
GET /healthz
  → POST /command/run
  → 持久化响应中的 task_id
  → GET /command/result/<task_id> 轮询状态
  → completed/failed 后读取 /command/result/<task_id>/log
```

任务提交接口只负责入队，正常返回 HTTP `202`，不会等待命令执行完成。因此 HTTP
客户端本身只需设置较短的请求超时，任务运行时间由 Worker 单独管理。

## 命令任务 API

### 提交任务

```http
POST /command/run
Content-Type: application/json
```

Host 任务示例：

```bash
curl --noproxy '*' -sS \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "yuxd",
    "command": "python train.py",
    "device_num": 1,
    "cpu": 4,
    "memory": 8,
    "mem_unit": "GB",
    "est_time": 30,
    "target": {"type": "host"}
  }' \
  "$WORKER/command/run"
```

赶论文（高优先级）任务示例，加上 `"priority": 1` 即可。

请求字段：

| 字段 | 必填 | 默认值 | 含义 |
|---|---:|---:|---|
| `user_id` | 是 | — | Worker 宿主机上已存在的 Linux 用户；Host 命令以该用户运行 |
| `command` | 是 | — | 要执行的完整 Shell 命令 |
| `device_num` | 否 | `0` | 自动分配的设备数量，非负整数；`0` 表示不申请设备 |
| `device_ids` | 否 | `[]` | 指定设备，如 `["0","2"]` 或 `["235:0","235:2"]`；非空时优先于 `device_num` |
| `cpu` | 否 | `0` | CPU 核数，非负整数；`0` 表示不限制 |
| `memory` | 否 | `0` | 内存数量，非负整数；`0` 表示不限制 |
| `mem_unit` | 否 | `GB` | `GB` 或 `MB`，大小写不敏感 |
| `est_time` | 否 | `0` | 预计运行分钟数，仅用于队列 ETA 展示，不是超时 |
| `priority` | 否 | `0` | 队列优先级，取值 `0` 或 `1`（0=普通、1=赶论文）；数值越大越先执行（同级内按提交时间 FIFO）；超范围（<0、>1）或非整数由数据层拒绝，返回 400 |
| `target` | 否 | `{"type":"host"}` | 执行目标，见“执行目标”一节 |

成功响应：

```http
HTTP/1.1 202 Accepted
```

```json
{
  "task_id": "7c65d5ac21f4",
  "position": 1,
  "priority": 0,
  "target": {"type": "host"},
  "message": "任务已提交，队列位置 #1"
}
```

`devices` 在真正开始运行、资源分配完成后才会出现在任务状态中，格式为 Linux
设备号 `major:minor`，例如 `235:0`。

### 执行目标

#### Host

省略 `target`，或者传入：

```json
{"type": "host"}
```

命令通过 `bash -i -c` 执行，会加载目标用户的交互 Shell 环境，工作目录为该用户
的 HOME。Worker 在启动进程前将其加入资源沙盒并切换到 `user_id`。

`command` 是完整 Shell 命令，调用方不得把未经处理的外部输入直接拼接进去。

#### 已运行的 Docker 容器

```json
{
  "user_id": "yuxd",
  "command": "python train.py --epochs 10",
  "device_ids": ["0"],
  "cpu": 4,
  "memory": 8,
  "mem_unit": "GB",
  "target": {
    "type": "docker_existing",
    "container": "training-01",
    "workdir": "/workspace",
    "user": "1000:1000",
    "env": {
      "RUN_ID": "experiment-42",
      "PYTHONUNBUFFERED": "1"
    }
  }
}
```

`docker_existing` 的 `target` 字段：

| 字段 | 必填 | 含义 |
|---|---:|---|
| `type` | 是 | 固定为 `docker_existing` |
| `container` | 是 | 已运行容器的名称或 ID |
| `workdir` | 否 | 容器内绝对路径 |
| `user` | 否 | Docker Exec 使用的容器用户；省略时使用容器默认用户 |
| `env` | 否 | 传给 Docker Exec 的环境变量对象，最多 128 项 |

容器任务还有以下要求：

- `user_id` 仍必须是 Worker 宿主机上存在的用户，用作任务和沙盒 owner；
- 必须通过 `device_num` 或 `device_ids` 申请至少一个设备；
- 容器必须处于 running 且未 paused；
- 容器创建时必须已经挂载可能申请的设备节点，Worker 不会向运行中的容器热添加
  `/dev/davinciN` 或 `/dev/nvidiaN`；
- 容器内需要 `/bin/sh`；命令最终通过 `/bin/sh -c` 执行；
- Docker 连接、容器状态或设备可见性错误通常在异步执行阶段出现：提交仍可能返回
  `202`，随后任务状态变成 `failed`。

### 查询队列

```http
GET /command/queue
```

```bash
curl --noproxy '*' -sS "$WORKER/command/queue"
```

响应示例：

```json
{
  "queue": [
    {
      "task_id": "7c65d5ac21f4",
      "user_id": "yuxd",
      "command": "python train.py",
      "status": "queued",
      "position": 1,
      "priority": 0,
      "cpu": 4,
      "est_time": 30,
      "eta": 0,
      "mem": "8G",
      "device_num": 1,
      "devices": [],
      "target": {"type": "host"},
      "created_at": 1786740000.25,
      "started_at": null,
      "finished_at": null
    }
  ],
  "total_pending": 1
}
```

`queue` 包含所有用户的 running、queued 任务以及最近 completed/failed 任务，不含
日志和退出结果。`total_pending` 只统计 queued，不包含 running。

`eta` 的单位是分钟，只在 queued 任务上计算；它是前方排队任务 `est_time` 的
简单累加，不包含正在运行任务的剩余时间，因此只能用于展示，不能作为调度保证。

排队顺序固定为 `priority` 降序 → `created_at` 升序：数值越大越先执行，
当前 0=普通、1=赶论文，赶论文任务永远先于普通任务执行，同优先级内按
提交时间 FIFO；`position` 是这一顺序下的全局排位。

### 查询单个任务

```http
GET /command/result/<task_id>
```

```bash
curl --noproxy '*' -sS \
  "$WORKER/command/result/7c65d5ac21f4"
```

完成后的响应示例：

```json
{
  "task_id": "7c65d5ac21f4",
  "user_id": "yuxd",
  "command": "python train.py",
  "status": "completed",
  "position": 1,
  "priority": 0,
  "cpu": 4,
  "est_time": 30,
  "eta": null,
  "mem": "8G",
  "device_num": 1,
  "devices": ["235:0"],
  "target": {"type": "host"},
  "created_at": 1786740000.25,
  "started_at": 1786740002.1,
  "finished_at": 1786740120.8,
  "result": {
    "returncode": 0,
    "timed_out": false,
    "error": null
  }
}
```

任务状态只有四种：

| 状态 | 含义 |
|---|---|
| `queued` | 已持久化，等待队首资源可用 |
| `running` | 已分配沙盒并开始执行 |
| `completed` | 进程正常结束且退出码为 `0` |
| `failed` | 非零退出、超时、取消、Docker 错误或沙盒清理失败 |

时间字段是 Unix 时间戳（秒，可能带小数）。任务不存在时返回 HTTP `404`。
标准输出和标准错误不放在此响应中，应通过日志接口读取。

### 读取任务日志

```http
GET /command/result/<task_id>/log
```

日志在任务运行期间持续写入，可以边运行边读取。

| Query | 含义 |
|---|---|
| 无参数 | 返回当前完整日志的 JSON |
| `tail=N` | 返回末尾 N 字节 |
| `offset=N&limit=M` | 从字节偏移 N 开始，最多读取 M 字节 |
| `raw=1` | 返回 `text/plain`，可与 `tail`、`offset`、`limit` 组合 |

```bash
# 完整 JSON
curl --noproxy '*' -sS \
  "$WORKER/command/result/7c65d5ac21f4/log"

# 末尾 4096 字节纯文本
curl --noproxy '*' -sS \
  "$WORKER/command/result/7c65d5ac21f4/log?tail=4096&raw=1"

# 分段读取
curl --noproxy '*' -sS \
  "$WORKER/command/result/7c65d5ac21f4/log?offset=0&limit=65536"
```

JSON 响应：

```json
{
  "data": "epoch 1...\n",
  "offset": 0,
  "total_size": 123456
}
```

范围参数按字节计算。日志尚未创建时返回空内容和 HTTP `200`；日志接口本身不会
判断任务是否存在，因此查询错误的 `task_id` 也会得到空日志。

### 删除或取消任务

```http
POST /command/tasks/delete
Content-Type: application/json
```

```bash
curl --noproxy '*' -sS \
  -H 'Content-Type: application/json' \
  -d '{"task_ids":["7c65d5ac21f4","1d835e5f721a"]}' \
  "$WORKER/command/tasks/delete"
```

```json
{
  "deleted": 2,
  "message": "已删除 2 个任务"
}
```

- queued、completed、failed：删除任务记录和对应日志；
- running：发起异步取消并销毁沙盒，最终任务保留为 `failed`，日志保留；
- `deleted` 表示本次请求处理的 ID 数量，不应被用来证明每个 ID 原来都存在。

## 队列行为

- 每个 Worker 有一个独立 FIFO 队列，不同 Worker 之间不共享状态。
- 队首任务资源不足时会等待并阻塞后面的任务，不会跳过队首进行回填调度。
- 资源允许时可以同时运行多个任务；FIFO 控制的是资源分配准入顺序。
- queued 和 running 状态持久化在 Worker SQLite 中。
- Worker 重启后，queued 任务重新入队；重启前处于 running 的任务标记为
  `failed`，错误信息为 Worker 可能在执行过程中重启。
- `NEU_BOX_COMMAND_TIMEOUT=0` 表示任务运行时间不限制；正数表示统一超时秒数。
- 完成记录和队列返回数量分别受 `NEU_BOX_COMMAND_MAX_COMPLETED`、
  `NEU_BOX_COMMAND_QUEUE_RECENT` 控制。

## 节点状态 API

### 服务信息

```http
GET /
```

```json
{"service":"neu-box-worker","version":"0.3.1"}
```

### 健康检查

```http
GET /healthz
```

```json
{
  "status": "ok",
  "role": "worker",
  "api_version": 1,
  "version": "0.3.1",
  "schema_version": 3
}
```

`200` 只表示 HTTP 服务和数据库 schema 已就绪，不表示一定有空闲设备。

### 资源状态

```http
GET /status
```

```json
{
  "status": "online",
  "total_cpu": 192,
  "idle_cpu": 98.7,
  "total_mem": 1080688844800,
  "idle_mem": 1030851747840,
  "total_devices": 8,
  "idle_devices": 7,
  "dev_status": {"0": 1, "1": 0},
  "active_sandboxes": 1,
  "api_version": 1
}
```

内存单位是字节；`idle_cpu` 是百分比；`dev_status` 中 `0` 表示空闲，`1` 表示
忙碌，JSON 对象中的设备号键为字符串。

## 终端沙盒 API

这些接口用于把已经存在的进程加入设备沙盒，不经过命令队列。第三方任务调度系统
一般只需使用 `/command/*`。

### 申请终端沙盒

```http
POST /sandbox/acquire
```

Host 进程：

```json
{
  "username": "yuxd",
  "pid": 45678,
  "device_num": 1,
  "device_ids": [],
  "cpu": 4,
  "memory": 8,
  "mem_unit": "GB"
}
```

容器内进程：

```json
{
  "username": "yuxd",
  "pid": 1316,
  "container": "training-01",
  "device_num": 1,
  "cpu": 0,
  "memory": 0,
  "mem_unit": "GB"
}
```

不提供 `container` 时，`pid` 是宿主机 PID，并校验它属于 `username`。提供
`container` 时，`pid` 是容器 PID namespace 中看到的 PID，Worker 会映射并
核验宿主机 PID。成功返回 HTTP `201`：

```json
{
  "sandbox_name": "sbx_yuxd_45678.slice",
  "devices": ["235:0"],
  "message": "PID 45678 已加入沙盒 sbx_yuxd_45678.slice，独占设备 ['235:0']"
}
```

该接口资源不足时直接返回 `503`，不会排队。

### 释放终端沙盒

Host：

```http
POST /sandbox/release
```

```json
{"sandbox_name":"sbx_yuxd_45678.slice"}
```

容器终端必须同时提供容器内 Shell PID 和本次 HTTP 客户端 PID：

```json
{
  "sandbox_name": "sbx_yuxd_45678.slice",
  "container": "training-01",
  "pid": 1316,
  "client_pid": 1488
}
```

Worker 会先把 Shell 和 HTTP 客户端迁回原 Docker cgroup，再销毁沙盒；销毁沙盒
会终止仍留在其中的其他进程。

### 加入已有沙盒

```http
POST /sandbox/join
```

```json
{
  "username": "yuxd",
  "pid": 45678,
  "sandbox_name": "sbx_yuxd_12345.slice"
}
```

该接口用于宿主机 PID，要求 PID 属于 `username`，并且沙盒名称中的 owner 与
`username` 相同。

### 查询沙盒

```http
GET /sandbox/list
GET /sandbox/list?username=yuxd
GET /sandbox/list?username=yuxd&container=training-01&pid=1316
```

```json
{
  "sandboxes": [
    {
      "name": "sbx_yuxd_45678.slice",
      "owner": "yuxd",
      "cpu": 4,
      "mem": "8G",
      "devices": ["235:0"],
      "created_at": 1786740000.25,
      "pids": [45678]
    }
  ],
  "current_sandbox": null
}
```

`username` 只过滤返回列表。`container` 和 `pid` 必须同时提供，用于查询该容器
进程当前所在的沙盒，并通过 `current_sandbox` 返回。

## HTTP 状态码与错误格式

普通错误：

```json
{"error":"user_id 不能为空"}
```

Docker 终端错误还可能包含机器可读的 `code`：

```json
{
  "error": "目标容器不存在: training-01",
  "code": "docker_container_not_found"
}
```

| 状态码 | 含义 |
|---:|---|
| `200` | 查询、释放、删除或加入成功 |
| `201` | 终端沙盒创建成功 |
| `202` | 命令任务成功入队 |
| `400` | JSON 字段缺失、类型错误或目标参数不合法 |
| `403` | Host PID 与用户名不匹配，或沙盒 owner 不匹配 |
| `404` | 任务或 Docker 容器不存在 |
| `409` | 容器身份发生变化、设备不可见或容器终端已在沙盒中 |
| `500` | cgroup、进程迁移、日志读取或沙盒销毁失败 |
| `503` | 即时沙盒资源不足，或 Docker 服务不可用 |

对于 `/command/run`，HTTP `202` 只代表成功入队。执行阶段的非零退出码、超时、
Docker 错误和清理错误都通过任务的 `status=failed`、`result.returncode` 和
`result.error` 报告。

## Python 接入示例

下面示例依赖 `requests`，显式忽略系统代理，提交一个任务并等待结束：

```python
import time

import requests


worker = "http://127.0.0.1:59075"
session = requests.Session()
session.trust_env = False

response = session.post(
    f"{worker}/command/run",
    json={
        "user_id": "yuxd",
        "command": "python train.py",
        "device_num": 1,
        "cpu": 4,
        "memory": 8,
        "mem_unit": "GB",
        "est_time": 30,
    },
    timeout=10,
)
response.raise_for_status()
task_id = response.json()["task_id"]

while True:
    response = session.get(
        f"{worker}/command/result/{task_id}",
        timeout=10,
    )
    response.raise_for_status()
    task = response.json()
    if task["status"] in {"completed", "failed"}:
        break
    time.sleep(2)

log = session.get(
    f"{worker}/command/result/{task_id}/log",
    params={"raw": 1},
    timeout=10,
)
log.raise_for_status()
print(log.text)

if task["status"] != "completed":
    raise RuntimeError(task["result"])
```

生产接入还应持久化 Worker 地址、`task_id`、业务任务 ID 的对应关系，并为查询请求
增加有限重试和退避。当前 API 没有服务端业务幂等键，业务系统应在自身数据库中
避免重复提交。
