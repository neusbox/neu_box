# Worker HTTP API

本文面向不使用 `neu-sbox`、直接接入 Neu Box Worker 的后端系统，适用于
Neu Box `0.5.0`。Worker 默认监听 `http://<worker-host>:59075`，所有接口均
返回 UTF-8；除纯文本日志接口外，请求和响应使用 JSON。

`neu-sbox` 只是这些接口的客户端封装，不是调用 Worker 的必要条件。

接口总览：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/` | 查询服务名称和版本 |
| `GET` | `/healthz` | 健康检查、API 版本和数据库 schema 版本 |
| `GET` | `/status` | 查询 CPU、内存、设备和沙盒状态 |
| `GET` | `/maintenance` | 查询 Worker 暂停及静止状态 |
| `POST` | `/maintenance/pause` | 暂停接收新任务及分配 sandbox（仅本机） |
| `POST` | `/maintenance/resume` | 恢复创建新 sandbox（仅本机） |
| `POST` | `/tasks` | 异步提交命令任务 |
| `GET` | `/tasks` | 查询队列和最近任务 |
| `GET` | `/tasks/<task_id>` | 查询任务状态和退出结果 |
| `GET` | `/tasks/<task_id>/log` | 读取实时任务日志 |
| `DELETE` | `/tasks` | 删除或取消任务 |
| `POST` | `/sandbox/acquire` | 为现有进程排队申请终端沙盒 |
| `GET` | `/sandbox/acquire/<acquire_id>` | 查询终端沙盒申请 |
| `POST` | `/container/register` | 容器归属登记（由节点 OCI runtime hook 调用，非用户接口） |
| `POST` | `/sandbox/release` | 销毁终端沙盒，释放设备 |
| `POST` | `/sandbox/join` | 将 Host PID 加入已有沙盒 |
| `GET` | `/sandbox/status` | 按 Host PID 或已登记容器查询沙盒 |
| `GET` | `/sandbox/list` | 查询终端沙盒 |

## 接入前须知

- Worker API 当前没有认证、签名和权限隔离，只能部署在可信内网。
- Worker 服务以 root 运行。`user_id` 是任务的执行身份，不是认证凭据；调用方
  可以指定 Worker 宿主机上任意已存在的用户。
- 直接调用 Worker 时不传 `node_id`。`node_id` 是 WebUI 转发请求时使用的字段，
  不属于 Worker API。
- 命令任务使用 `/tasks` 资源接口并持久化；终端沙盒使用 `/sandbox/*`。
  两种申请共用 Worker 调度队列，避免绕过已经排队的任务抢占设备。
- 当前没有 API 版本前缀、幂等键、回调或 Webhook。接入方应记录 `task_id` 并
  轮询结果；不要在响应不确定时盲目重试提交，否则可能产生重复任务。

以下示例统一使用：

```bash
WORKER=http://127.0.0.1:59075
```

如果机器配置了 HTTP 代理，访问内网 Worker 时应绕过代理，例如使用
`curl --noproxy '*'`。

## API 版本

`/healthz` 与 `/status` 均返回 `api_version`（当前 `2`）：

- 仅破坏性变更（删除字段、改变语义）时 +1；新增字段/端点不升版本
- 接入方应拒绝 `api_version < 2` 的连接；v1 使用的 `/command/*` 路径已移除
- 旧版 worker 不上报该字段；由于缺少 `/tasks`，接入方应拒绝连接

## 队列接入流程

推荐的最小接入流程是：

```text
GET /healthz
  → POST /tasks
  → 持久化响应中的 task_id
  → GET /tasks/<task_id> 轮询状态
  → completed/failed 后读取 /tasks/<task_id>/log
```

任务提交接口只负责入队，正常返回 HTTP `202`，不会等待命令执行完成。因此 HTTP
客户端本身只需设置较短的请求超时，任务运行时间由 Worker 单独管理。
Worker 暂停期间返回 HTTP `503`，响应中的 `code` 为 `worker_paused`，不创建任务记录或入队。

## 命令任务 API

### 提交任务

```http
POST /tasks
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
  "$WORKER/tasks"
```

赶论文（高优先级）任务示例，加上 `"priority": 1` 即可。

请求字段：

| 字段 | 必填 | 默认值 | 含义 |
|---|---:|---:|---|
| `user_id` | 是 | — | Worker 宿主机上已存在的 Linux 用户；Host 命令以该用户运行 |
| `command` | 是 | — | 要执行的完整 Shell 命令 |
| `device_num` | 否 | `0` | 自动分配的设备数量，非负整数；`0` 表示不申请设备 |
| `device_ids` | 否 | `[]` | 指定设备；推荐只传 minor，如 `["0","2"]`；也接受与本机设备完全一致的 `major:minor`；非空时优先于 `device_num` |
| `cpu` | 否 | `0` | CPU 核数，非负整数；`0` 表示不限制 |
| `memory` | 否 | `0` | 内存数量，非负整数；`0` 表示不限制 |
| `mem_unit` | 否 | `GB` | `GB` 或 `MB`，大小写不敏感 |
| `est_time` | 否 | `0` | 预计运行分钟数，仅用于队列 ETA 展示，不是超时 |
| `priority` | 否 | `0` | 队列优先级，取值 `0` 或 `1`（0=普通、1=赶论文）；数值越大越先执行（同级内按提交时间 FIFO）；超范围（<0、>1）或非整数由数据层拒绝，返回 400 |
| `target` | 否 | `{"type":"host"}` | 执行目标，见“执行目标”一节 |

`user_id` 会在入队前通过宿主机用户数据库校验。用户不存在时返回 HTTP `400`
和 `{"error":"系统用户 <name> 不存在"}`，任务不会进入队列。

设备节点的 major 不是 API 常量。Worker 根据 `NEU_BOX_DEVICE_FILTER` 匹配本机
实际设备节点，并通过 `stat(2)` 取得设备号。调用方应优先只传 minor；若传
`major:minor`，必须与 Worker 当前发现的完整设备号一致。非空的 `device_ids`
必须是数组；包含不存在的设备时返回 HTTP `400`。

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
设备号 `major:minor`，例如 `<actual-major>:0`；其中 major 是 Worker 本机动态发现
的实际值，调用方不能假设它固定为 `235`。

### 执行目标

#### Host

省略 `target`，或者传入：

```json
{"type": "host"}
```

命令通过 `bash -i -c` 执行，会加载目标用户的交互 Shell 环境，工作目录为该用户
的 HOME。Worker 在启动进程前将其加入资源沙盒并切换到 `user_id`。

`command` 是完整 Shell 命令，调用方不得把未经处理的外部输入直接拼接进去。
Worker 在进程管道层合并 stdout 和 stderr，因此 Bash 初始化错误、语法解析错误、
`command not found`、权限错误及程序写入 stderr 的内容都会进入任务日志。Shell
返回非零时任务状态为 `failed`，具体报错读取日志，退出码读取 `result.returncode`。

#### 一次性 Docker 容器

```json
{
  "user_id": "yuxd",
  "command": "python train.py --epochs 10",
  "device_ids": ["0"],
  "cpu": 4,
  "memory": 8,
  "mem_unit": "GB",
  "target": {
    "type": "docker",
    "image": "pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime",
    "workdir": "/workspace",
    "user": "1000:1000",
    "env": {
      "RUN_ID": "experiment-42",
      "PYTHONUNBUFFERED": "1"
    }
  }
}
```

`docker` 目标的字段：

| 字段 | 必填 | 含义 |
|---|---:|---|
| `type` | 是 | 固定为 `docker` |
| `image` | 是 | 镜像名，可带 registry / tag / digest |
| `workdir` | 否 | 容器内绝对路径 |
| `user` | 否 | 容器内以哪个用户运行 |
| `env` | 否 | 环境变量对象，最多 128 项 |

容器任务还有以下要求：

- `user_id` 仍必须是 Worker 宿主机上存在的用户，用作任务和沙盒 owner；
- 必须通过 `device_num` 或 `device_ids` 申请至少一个设备 —— 没有设备的容器拿不到
  任何 NPU 授权，起容器没有意义；
- 容器是一次性的：Worker 用 `docker run` 起、跑完删除。**不支持在已有容器里
  执行**：容器的归属登记必须发生在它第一个 NPU 进程之前，别人已经起好的容器
  没有这个时机，只能拒绝；
- `command` 是**参数表**不是 shell 字符串：Worker 把它交给 Docker 时会按 shell
  引用规则拆成 argv（docker-py 的 `split_command`）。要跑多语句或重定向，自己写
  `sh -c '...'`，例如 `"sh -c 'echo hi; sleep 1'"`；直接写裸脚本会被拆成
  `["out=$(", "(", …]` 去 exec，报 `executable file not found`；
- 全部受管设备节点都会挂进容器 —— 限制由 BPF 逐卡判定，不靠 `/dev` 里有没有
  节点，而驱动本身又必须拿到 manager / hdc 才能初始化；
- 容器里不需要任何客户端：Worker 只把沙盒名写成 `sandbox_cgroup` annotation，
  由节点级 OCI runtime hook 在容器 ENTRYPOINT 之前登记 mount namespace。Worker
  自己**不补登记** —— 登记晚了驱动已经按空权限建过 UDA 设备表，补也修不回来；
- cpu/memory 限额落在容器的 docker flag 上，语义是"每个容器一份"，与 Host 目标的
  "整个沙盒合计一份"不同；
- 镜像不存在、Docker 不可用、容器没在 runtime hook 里登记（`docker_runtime_not_registered`）
  等错误出现在异步执行阶段：提交仍可能返回 `202`，随后任务状态变成 `failed`。

### 查询队列

```http
GET /tasks
```

```bash
curl --noproxy '*' -sS "$WORKER/tasks"
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

`queue` 是**统一队列视图**：命令任务 + acquire 会话，两类条目都在里面，靠
`kind`（`task` / `acquire`）区分。顺序是"在跑的"（任务的 `running`、会话的
`active`）→"排队中的"（任务的 `queued`、会话的 `queued`/`allocating`，两类一起
按优先级降序、提交时间升序编号 `position`/`eta`）→ 最近结束的（两类各取最近
`NEU_BOX_COMMAND_QUEUE_RECENT` 条，按结束时间倒序合并）。

可用 `?kind=task|acquire`、`?state=<状态>` 过滤。任务条目沿用原来的字段（另加
`kind`/`id`）；acquire 条目没有 `command`/日志/`returncode`，它的字段是
`id`(=request_id)、`status`(会话状态)、`user_id`、`pid`、`device_num`、
`device_ids`、`devices`、`priority`、`sandbox_name`、`code`、`created_at`、
`started_at`(=借出时间)、`finished_at`。消费方按 `kind` 分支渲染即可 ——
以前 acquire 根本不在这个列表里（排队中的会话甚至没有列表接口）。

`queue` 包含所有用户的 running、queued 任务以及最近 completed/failed 任务，不含
日志和退出结果。`total_pending` 只统计 queued，不包含 running。

`eta` 的单位是分钟，只在 queued 任务上计算；它是前方排队任务 `est_time` 的
简单累加，不包含正在运行任务的剩余时间，因此只能用于展示，不能作为调度保证。

排队顺序按 `priority` 降序、`created_at` 升序排列。当前优先级为 0=普通、
1=赶论文，同优先级内按提交时间 FIFO；`position` 是这一顺序下的全局排位 ——
**任务和 acquire 会话一起编号**（以前只数任务，master 看到的排位会和队列对不上）。

真正决定"先跑谁"的是 **first-schedulable**：队列按上述顺序扫描，取**第一个当前
资源足够**的条目。队首在等设备（卡被占满 / 指定卡未释放）时不会阻塞后面不需要
这张卡的条目。

### 查询单个任务

```http
GET /tasks/<task_id>
```

```bash
curl --noproxy '*' -sS \
  "$WORKER/tasks/7c65d5ac21f4"
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
  "devices": ["<actual-major>:0"],
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

任务状态有五种：

| 状态 | 含义 |
|---|---|
| `queued` | 已持久化，等待有可用资源时被调度 |
| `running` | 已分配沙盒并开始执行 |
| `completed` | 进程正常结束且退出码为 `0` |
| `failed` | 非零退出、超时、Docker 错误或沙盒清理失败 |
| `cancelled` | 被用户取消（排队中取消或运行中取消）；记录与日志**保留** |

时间字段是 Unix 时间戳（秒，可能带小数）。任务不存在时返回 HTTP `404`。
标准输出和标准错误不放在此响应中，应通过日志接口读取。

### 读取任务日志

```http
GET /tasks/<task_id>/log
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
  "$WORKER/tasks/7c65d5ac21f4/log"

# 末尾 4096 字节纯文本
curl --noproxy '*' -sS \
  "$WORKER/tasks/7c65d5ac21f4/log?tail=4096&raw=1"

# 分段读取
curl --noproxy '*' -sS \
  "$WORKER/tasks/7c65d5ac21f4/log?offset=0&limit=65536"
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

### 删除或取消任务（批量兼容入口）

```http
DELETE /tasks
Content-Type: application/json
```

```bash
curl --noproxy '*' -sS \
  -X DELETE \
  -H 'Content-Type: application/json' \
  -d '{"task_ids":["7c65d5ac21f4","1d835e5f721a"]}' \
  "$WORKER/tasks"
```

```json
{
  "deleted": 2,
  "message": "已删除 2 个任务"
}
```

- **queued / running：取消**。终态是 `cancelled`，**记录与日志都保留**（取消是
  历史的一部分，不再"删掉就当没发生"）；running 的任务会异步取消并销毁沙盒；
- **completed / failed / cancelled：删除**记录与对应日志（这才是真正的"删除"）；
- `deleted` 表示本次请求处理的 ID 数量，不应被用来证明每个 ID 原来都存在。

### 取消单个条目（任务或 acquire 会话）

```http
DELETE /tasks/<id>?kind=task|acquire
Content-Type: application/json
```

```bash
# 取消一个还在排队的命令任务
curl --noproxy '*' -sS -X DELETE "$WORKER/tasks/7c65d5ac21f4?kind=task"

# 取消一个 acquire 会话：还没拿到卡就摘出队列，已经拿到卡就就地释放
curl --noproxy '*' -sS -X DELETE \
  -H 'Content-Type: application/json' \
  -d '{"host_pid": 12345}' \
  "$WORKER/tasks/9f0a1b2c3d4e?kind=acquire"
```

`kind` 缺省为 `task`。响应 `{"status": ...}`：

| status | 含义 |
|---|---|
| `cancelled` | 排队中的条目被摘出队列（任务留痕 `cancelled`；会话账本写 `cancelled`），永远不会被执行 |
| `cancelling` | 运行中的任务已发取消信号，终态由执行体落成 `cancelled` |
| `released` | acquire 已经拿到卡：**在同一个调用里**完成释放（归还借出的进程、收掉长出来的进程与容器、还卡），响应里带 `sandbox_name` |
| `deleted` | 该任务已经是终态，按"删除"处理（记录与日志删除） |

id 既不在队列里也不在运行中 → `404`；`kind` 不是 task/acquire 或 `host_pid`
不是整数 → `400`。重复取消天然幂等（第二次得到 404）。

`host_pid` 只有 acquire 用得上，而且只在"已经拿到卡"时起作用：调用方可能是被借
出去的 shell 的**子进程**（`neubox acquire` 阻塞期间按下 Ctrl-C 的 neubox 就是
这样，cgroup 成员身份随 fork 继承），Worker 会先把它搬回父进程的 origin 再销毁
沙盒 —— 否则它会被这次释放的 `cgroup.kill` 一起带走。

## 队列行为

- 每个 Worker 有一个独立优先级队列，不同 Worker 之间不共享状态。
- 队列按 first-schedulable 出队：从队首开始找第一个能拿到资源的任务，暂时拿不到
  资源的任务退避 1 秒后重新参与排队，不会阻塞后面的任务。
- 资源允许时可以同时运行多个任务；资源分配按优先级降序准入，同优先级内按
  提交时间 FIFO。
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
{"service":"neuboxd","version":"0.5.0"}
```

### 健康检查

```http
GET /healthz
```

```json
{
  "status": "ok",
  "role": "worker",
  "api_version": 2,
  "version": "0.5.0",
  "schema_version": 7
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
  "maintenance": {
    "pending_tasks": 0,
    "pending_acquires": 0,
    "running_tasks": 1,
    "running_acquires": 0,
    "running_total": 1,
    "running": 1,
    "dispatching": 0,
    "maintenance_errors": {},
    "pause_in_progress": false,
    "paused": false,
    "allocations_in_flight": 0,
    "sandbox_lifecycle": {
      "active": 1,
      "creating": 0,
      "destroying": 0,
      "residuals": []
    },
    "quiet": false
  },
  "api_version": 2
}
```

内存单位是字节；`idle_cpu` 是百分比；`dev_status` 中 `0` 表示空闲，`1` 表示
忙碌，JSON 对象中的设备号键为字符串。

`maintenance` 是队列的维护快照：`pending_*` / `running_*` 分别是排队中和运行中
的任务与 acquire 数量，`dispatching` 是正在调度中的请求数，`sandbox_lifecycle`
按数据库状态统计沙盒数量，`residuals` 是数据库或 cgroup 里还看得见的沙盒名。

`active_sandboxes` 来自原生 sandbox CLI 的 `list` 输出；查询失败时当前实现返回
`0`，因此它只适合状态展示，不能单独作为部署或升级时的隔离层验收依据。

### 维护暂停

```http
POST /maintenance/pause
GET /maintenance
POST /maintenance/resume
```

pause/resume 只接受 loopback 请求，只设置暂停状态并返回，不等待任务结束，也不
停服、备份或清理 BPF。pause 不清空 tasks 的 pending 队列，不中断已经运行的任务，
但会取消仍在排队的 acquire（以 `worker_paused` 失败结束，恢复后需重新申请）；
`POST /tasks` 和 `POST /sandbox/acquire` 均拒绝新建请求，返回 `503 worker_paused`。
显式调用过 `POST /maintenance/pause` 后，`POST /maintenance/resume` 返回
`409 maintenance_in_progress`，直到 Worker 重启。

`quiet=true` 表示 Worker 已暂停，且没有运行任务、分配或调度中的请求，也没有
DB/cgroup/native state 残留沙盒；pending 任务不参与该判断。查询生命周期失败时
`quiet` 保持 `false`。全局 BPF 程序及 pins 不参与 `quiet` 判断，升级前仍需清理。

暂停标记持久化在数据库路径加 `.paused` 的文件中，Worker 重启后仍保持暂停，
resume 删除该标记并恢复调度。直接以 root 调用 `neuboxctl sandbox create`
属于管理操作，不经过 Worker 的 pause 闸门。

安装后的 `neuboxctl pause` 在调用暂停接口后，还会等待 `quiet=true`、备份数据库
及配置、执行 BPF cleanup，全部成功后才停服，供 RPM 升级使用。等待超时、备份或 cleanup
失败时，Worker 保持在线且暂停。该命令默认无限等待，可用 `--timeout <秒>` 限时，
不自动杀任务。完成后用 `neuboxctl setup` 启动新版 Worker；setup 在健康检查
通过后调用 resume。`neuboxctl resume` 本身不启动服务。

## 终端沙盒 API

这些接口用于把已经存在的进程加入设备沙盒。`acquire` 与命令任务共用调度队列，
第三方任务调度系统一般只需使用 `/tasks`。

### 申请终端沙盒

```http
POST /sandbox/acquire
```

Host 进程（`pid` 是宿主机 PID）：

```json
{
  "username": "yuxd",
  "pid": 45678,
  "device_num": 1,
  "device_ids": [],
  "cpu": 4,
  "memory": 8,
  "mem_unit": "GB",
  "priority": 0
}
```

`priority` 可选，语义与 `POST /tasks` 完全一致：取值 `0`（普通）或 `1`（赶论文），
默认 `0`，数值越大越先拿设备；超范围或非整数返回 `400`。acquire 与命令任务**共用
同一个调度队列**（同一个优先级顺序），只是抢到设备之后一个去 join 已有 PID、一个
去起新进程。

`pid` 必须是宿主机 PID，并校验它属于 `username`。容器不走 `acquire`：容器本身
不持有授权，也不搬进沙盒 cgroup，由节点级 OCI runtime hook 在启动时登记归属
（见 `/container/register`）。

成功返回 HTTP `201`：

```json
{
  "sandbox_name": "sbx_yuxd_45678.slice",
  "devices": ["<actual-major>:0"],
  "message": "PID 45678 已加入沙盒 sbx_yuxd_45678.slice，独占设备 ['<actual-major>:0']"
}
```

资源不足时请求留在 Worker 调度队列中，接口立即返回 `202` 和 `acquire_id`，调用方
轮询 `GET /sandbox/acquire/<acquire_id>`；分配成功并把 PID 加入沙盒后返回 `201`。
Worker 已暂停时返回 `503 worker_paused`。目标 PID 已在终端沙盒时返回 `409`，调用方
必须先显式 release，Worker 不会隐式销毁旧沙盒。

### 释放终端沙盒

```http
POST /sandbox/release
```

```json
{"sandbox_name":"sbx_yuxd_45678.slice"}
```

`release` 只接收沙盒名，销毁整个沙盒：归还 acquire 借出去的那个终端、收掉沙盒
里长出来的进程，并**停掉**挂靠的容器（撤登记 → `docker stop` → 等容器真的退出 →
放 pin）。**只停不删** —— 删容器会连可写层一起销毁，用户可能还要 commit / cp
出来；容器留着，下次 `docker start` 重新走 hook，登记被拒就起不来。是否涉及容器
由 Worker 在内部判断，调用方不需要报容器。

acquire 会话在 Worker 侧有一份**账本**（`sessions` 表）：`queued` → `allocating`
→ `active` → `released`，旁路 `cancelled`（客户端取消 / 停机维护批量取消）与
`failed`（校验或 join 失败，`code` 说明原因）。中间态照记，但**运行时逻辑不依赖
它** —— 调度看的是内存队列与沙盒的实际状态；进程异常退出后只做"修表"：把没有
终态的行标成 `interrupted`（不重建请求、不重跑校验），而 `active` 且沙盒仍在的
会话保持原样并在启动时重新装载为"运行中的 acquire"。账本可以用
`GET /tasks?kind=acquire` 查询（含历史）。

可选的 `host_pid` 是**调用方自己的 PID**（`{"sandbox_name":"…","host_pid":45678}`）：
调用方可能是被借出去的那个 shell 的**子进程**（`neubox release` 就是这样，
cgroup 成员身份随 fork 继承），而销毁的最后一步是 `cgroup.kill` —— 不先把它
搬出去，它会跟着自己这次 release 一起被杀（表现为 `zsh: killed`、退出码 137）。
给了 `host_pid` 时 Worker 会在销毁前把它搬回**它父进程的 origin**（也就是被借
进程原来的 cgroup），只搬它自己：它的子树与"沙盒里长出来的进程"照旧随沙盒
一起收掉。搬不动（不在本沙盒、父进程没有 origin）不算错误，不影响 release
本身的语义。

### 加入已有沙盒

```http
POST /sandbox/join
```

把 host PID 写进沙盒 cgroup：

```json
{
  "username": "yuxd",
  "pid": 45678,
  "sandbox_name": "sbx_yuxd_12345.slice"
}
```

要求 PID 属于 `username`，并且沙盒名称中的 owner 与 `username` 相同。

### 登记容器归属（runtime hook 专用）

```http
POST /container/register
```

```json
{
  "container_id": "5f1c…",
  "host_pid": 1316,
  "sandbox_cgroup": "sbx_yuxd_12345.slice",
  "container_cgroup": "/system.slice/docker-5f1c….scope",
  "mount_namespace": 4026533001
}
```

这个端点**不是给用户调的**：调用方是节点上的 OCI runtime hook（`neu-box-hook`），
它在容器 ENTRYPOINT 之前把可信的运行时身份交上来，Worker 才是唯一写 BPF map 和
数据库的一方。沙盒名来自 Docker 的 `sandbox_cgroup` annotation，而容器侧的
annotation 由谁写、怎么走，见 `docs/container-registration.md`；**本节是接口的
准**（字段、状态码、错误码），那份文档讲 Worker 侧的流程，不复述这里的表。

容器只报身份，不报归属：cgroup 路径、mount namespace 和 init host PID 都由
Worker 自己读 `/proc/<pid>` 解析（**不查 Docker API** —— hook 跑在 Docker 的
create 路径里，从那里调 Docker 会重入授权插件）。`container_cgroup` /
`mount_namespace` 只做交叉验证，与 Worker 读到的不一致返回 `409`；与宿主机共用
mount namespace 的 PID 一律拒绝，否则等于把整机登记成受托方。

容器不持有授权，只是**受托方**：它借的是它挂上的那个沙盒那一份；同一个 mount
namespace 已经登记在别的沙盒（或别的 container_id）下时返回 `409`。成功返回
HTTP `201`：

```json
{
  "sandbox_name": "sbx_yuxd_12345.slice",
  "container_id": "5f1c…",
  "mount_namespace": 4026533001,
  "container_cgroup": "/system.slice/docker-5f1c….scope",
  "status": "registered"
}
```

同一身份重复登记是幂等的（hook 可能重试）：返回 HTTP `200` 和
`"status":"already_registered"`，不会重复写入。登记必须发生在容器里第一个 NPU
进程之前 —— 驱动在第一次初始化时按当时权限建 UDA 设备表并按 mount namespace
缓存复用，登记晚了会建出一张空表且事后无法修复。注销不设端点：容器退出由 Worker
的 pidfd 监听即时发现，沙盒销毁时也会一并停掉它名下的容器（不删，可写层留给
用户；见 [`isolation.md`](isolation.md)）。

错误响应：

| 状态 | `code` | 含义 |
|---:|---|---|
| `400` | — | `container_id` / `host_pid` / `sandbox_cgroup` 缺失或非法 |
| `404` | `sandbox_not_found` | 沙盒名不是数据库里的精确沙盒名 |
| `409` | `sandbox_not_active` | 沙盒正在销毁 |
| `409` | `docker_container_registered_elsewhere` | 该 mount namespace 已登记给别的沙盒或别的容器 |
| `409` | `docker_container_same_mount_namespace` | PID 与宿主机共用 mount namespace |
| `409` | `runtime_identity_changed` | hook 报的 cgroup / mnt ns 与 Worker 读到的不一致 |
| `409` | `docker_container_pid_invalid` | PID 不存在或已退出 |

hook 收到非 2xx（或超时/连不上）会退非 0，`runc create` 失败、容器不启动 ——
这是 fail-closed：没有登记的容器拿不到任何设备授权。

### 查询沙盒

宿主进程可用 `GET /sandbox/status?pid=<host-pid>` 查询；容器内使用私有 PID
namespace 时，应传已登记的 `GET /sandbox/status?container=<name-or-id>`，Worker
按容器登记记录返回所属 sandbox。两种形式均返回：

```json
{"sandbox_name": "sbx_yuxd_45678.slice", "sandbox": {...}}
```

```http
GET /sandbox/list
GET /sandbox/list?username=yuxd
```

```json
{
  "sandboxes": [
    {
      "name": "sbx_yuxd_45678.slice",
      "owner": "yuxd",
      "cpu": 4,
      "mem": "8G",
      "devices": ["<actual-major>:0"],
      "created_at": 1786740000.25,
      "pids": [45678],
      "state": "ACTIVE"
    }
  ]
}
```

`username` 只过滤返回列表（比对沙盒名的 owner 段），不改变返回内容。沙盒列表来自
Worker SQLite 记录；查询接口不会自动删除记录。

## HTTP 状态码与错误格式

普通错误：

```json
{"error":"user_id 不能为空"}
```

沙盒和容器登记相关的错误还可能包含机器可读的 `code`：

```json
{
  "error": "无法完整扫描或清理 sandbox sbx_yuxd_12345.slice 的 Docker 容器",
  "code": "docker_container_cleanup_failed"
}
```

| 状态码 | 含义 |
|---:|---|
| `200` | 查询、释放、删除或加入成功 |
| `201` | 终端沙盒创建成功，或容器归属登记成功 |
| `202` | 命令任务成功入队，或终端沙盒申请进入队列 |
| `400` | JSON 字段缺失、类型错误或目标参数不合法 |
| `403` | Host PID 与用户名不匹配，或沙盒 owner 不匹配 |
| `404` | 任务不存在，或沙盒名对不上（`sandbox_not_found`） |
| `409` | PID 已加入其他沙盒、沙盒正在销毁，或容器身份冲突 |
| `500` | cgroup、进程迁移、日志读取或沙盒销毁失败 |
| `503` | Worker 处于暂停维护，或 Docker 服务不可用 |

对于 `POST /tasks`，HTTP `202` 只代表成功入队。执行阶段的非零退出码、超时、
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
    f"{worker}/tasks",
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
        f"{worker}/tasks/{task_id}",
        timeout=10,
    )
    response.raise_for_status()
    task = response.json()
    if task["status"] in {"completed", "failed"}:
        break
    time.sleep(2)

log = session.get(
    f"{worker}/tasks/{task_id}/log",
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
