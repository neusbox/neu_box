# 容器归属登记（Worker 侧）

容器怎么拿到沙盒的设备授权：**谁在什么时候调 `POST /container/register`、Worker
必须做什么、做不到会怎样**。

本文只讲 Worker 这一侧。跨仓库的两头不在这里：

| 想看什么 | 去哪 |
|---|---|
| 端点契约：请求字段、状态码、错误码、幂等语义 | [`worker-api.md`](worker-api.md)「登记容器归属（runtime hook 专用）」—— **接口以那边为准**，本文不重复字段表 |
| runtime 侧怎么做：wrapper 怎么注入、hook 怎么读 OCI state、`daemon.json` / `runtime.env` 怎么配、装机顺序为什么是硬的 | `neu_box_runtime/docs/runtime-hook.md` |
| 客户端 `neu-sbox` 怎么用、annotation 是谁拼的 | `neu_box_goClient/README.md` |

这是一条**跨仓库链路**：运行时的身份由 runtime 侧采集，授权判断和落库全在 Worker。
改协议字段之前先确认三边都能跟上，单边改动等于 break。

旧的容器内 gate（挂 neu-sbox、`/bin/sh -c 'kill -STOP $$'`、HTTP 握手、
ready/ack/release 九个端点）已整体废弃，见文末「已删除」。

## 模型

沙盒是授权方（owner），容器是受托方。容器**不搬进**沙盒 cgroup —— 它留在
Docker 自己的 cgroup 里，靠 mount namespace inode 把自己"挂"到某个沙盒名下：

```
container_owner[mnt_ns_inum] = sandbox_name
```

这张 BPF map 是唯一的强制点：没登记的容器，即使卡空着也一律拒绝设备
（fail-closed）。驱动按 mnt ns 建 UDA 设备表，我们的 key 和它的 key 同宽，
`stat("/proc/<pid>/ns/mnt").st_ino` 就是 BPF 里 CO-RE 读到的 `ns.inum`。

```
docker run --annotation sandbox_cgroup=<name> ...      ← 客户端 / 用户
        │
   dockerd → containerd → neu-box-runtime              ← runtime 侧：注入 hook
                              │
                          真 runc：建 namespace/cgroup
                              │
                        neu-box-hook                    ← runtime 侧：读 OCI state
                              │
                     POST /container/register           ← Worker 从这里开始
                              │
                    ┌─────────┴─────────┐
                 2xx/200            4xx/超时/连不上
                    │                    │
              hook 退 0              hook 退非 0
                    │                    │
            runc 继续，ENTRYPOINT 执行   runc create 失败，容器不启动
```

**Worker 是唯一写 BPF map 和数据库的人。** runtime 和 hook 不碰 BPF，只负责把
可信的运行时身份转交给 Worker。请求怎么发、超时多长、失败时 hook 退什么码，
都是 runtime 侧的事，见 `neu_box_runtime/docs/runtime-hook.md`。

## annotation

Worker 收到的 `sandbox_cgroup` 最终来自 Docker 的
`HostConfig.Annotations`，它会原样进 OCI `config.json` 的 `annotations`：

| 项 | 值 |
|---|---|
| 键 | `sandbox_cgroup` |
| 值 | **沙盒名**，如 `sbx_yuxd_123.slice` —— 精确等于数据库主键 |
| 位置 | Docker 的 `HostConfig.Annotations`（不在 `Config` 顶层） |

只接受沙盒名这一种形式。**不接受** cgroup 路径、不接受 basename：路径会被
回收复用，沙盒销毁重建后旧 annotation 会指到一个活的、别人的沙盒上。

docker-py 7.x **没有**一等参数，Worker 自己起容器时只能
`hc = api.create_host_config(); hc["Annotations"] = {...}` 这样塞进去，这是已知的
将就写法，别当 bug"修掉"（`src/neu_box/execution/docker.py`）。用户侧
`docker run --annotation sandbox_cgroup=<name>` 和 `neu-sbox docker run` 由
goClient 提供。

annotation 是**传输通道，不是凭证**。真正的校验在 Worker 侧 —— 见下面「这个入口
有多危险」。

## 为什么必须卡在 ENTRYPOINT 之前

登记必须发生在容器 ENTRYPOINT 之前 —— 驱动在容器内第一次 NPU 初始化时建表并
按 mnt ns 缓存，登记晚了会建出一张空表并被永久复用，事后补登记也修不回来。
OCI runtime hook 是唯一能卡在这个窗口里的点（Docker 的 create 路径里，
ENTRYPOINT 还没跑）。

这也是"登记不上就绝不放行"的全部理由：放行 = 容器带着一张空 UDA 表永久坏掉。
所以失败路径只能是 hook 退非 0、`runc create` 失败，不能是"降级放行"。

## Worker 侧必须做到的四条

1. **只读 `/proc`，不查 Docker API。** hook 跑在 Docker 的 create 路径里，从那里
   调 Docker API 会重入授权插件。
2. **拒绝与宿主机共用 mount namespace 的 PID。** 否则等于把整个宿主机登记成受托方。
3. **「读沙盒状态」和「写登记」在同一个 `SbxManager.lifecycle_lock()` 临界区里。**
   否则 `destroy_sandbox` 能在两步之间把沙盒拆掉。

   状态语义：

   | 沙盒状态 | 登记时怎么办 |
   |---|---|
   | `DESTROYING` | 拒（409 `sandbox_not_active`） |
   | `ACTIVE` | 直接用 |
   | `CREATING` | **登记即 join** —— 先推成 `ACTIVE`，再登记 |

   **CREATING 不能直接拒。** docker 命令任务的容器按设计**不搬进沙盒 cgroup**，
   所以它永远不调 `join_sandbox`，沙盒会一直停在 CREATING
   （`src/neu_box/runtime/sandbox.py` 的注释：`Keep CREATING until the first join`）。
   直接拒 = **每一个 docker 命令任务**都在 hook 处 409、容器创建失败。

   容器不搬进 cgroup，但"登记"在这套授权关系里就是"我加入这个沙盒"的等价动作。
4. **记录 `init_start_time`（`/proc/<pid>/stat` 第 22 字段）防 PID 复用**，
   并钉住 mnt ns fd 防 inum 回收 —— 沿用现有 `ContainerIdentity` 的做法。

   光有 mnt ns inum 分不清"同一个容器"和"回收后重用的号"，实测记录见
   `neu_box_runtime/docs/runtime-hook.md`「phase 验证记录」。

## 注销（没有端点）

注销不设端点，因为登记的同一时刻就已经把"怎么知道它死了"钉好了：登记顺手钉住
mnt ns fd、对 init `host_pid` 开 pidfd 挂进 epoll —— 容器一退出，收尸线程就被
事件叫醒并撤登记。**必须钉住 mnt ns fd**：不钉的话 inum 会被内核回收复用，
后来的新容器会白捡一份授权。

三条兜底路径：Worker 重启（fd 全丢）之后由 `reconcile_containers` 对账清掉残留
条目；沙盒销毁时也会一并收掉它名下的容器（撤登记 → `docker rm -f` → 等容器真的
退出 → 放 pin）；启动恢复和每轮收尸还会按 `neu-box.sandbox` label 全量扫一遍
Docker，收掉**沙盒记录已经不存在**的无主容器。`POST /sandbox/release` 的语义见
此：调用方不需要报容器。

第三条是给崩溃窗口兜底的：容器在 `docker create/start` 之后就带着 label，但
`containers` 行要等 runtime hook 登记才出现。Worker 若死在这两者之间，那个容器对
`reconcile_containers`（只遍历 `containers` 表）和"按沙盒名扫"的销毁路径都是不可见
的 —— label 是它唯一的持久句柄。所以这一遍清扫不看沙盒记录、只按 label 反查；它
跑在销毁路径**之外**，dockerd 不可用时只是下一轮重试，不会把任何一个沙盒的销毁
拖住。

## 这个入口有多危险

> **这是目前最危险的入口。** 其他 `/sandbox/*` 接口的语义是"操作我自己的沙盒"，
> 而这个接口收的是**一个裸 `host_pid`**，Worker 会去读它的 mnt ns 并写进授权表。
> 谁都能访问 Worker HTTP，谁就能 `POST {"host_pid": 1, "sandbox_cgroup": "<有卡的沙盒>"}`。
>
> 现在靠三条撑着：Worker 自己读 `/proc` 不信 hook 的自述、拒绝与宿主共用 mnt ns
> 的 PID、校验沙盒归属。**鉴权还没做**（见下）。做的时候这个端点要单独处理 ——
> 它是宿主机侧 root 进程调的，不该和面向用户的 `/sandbox/*` 用同一套门槛。

命名按**资源**走（容器登记），不按调用方 —— 否则 docker 调的接口就该叫
`/docker/*` 了。

## 这轮明确不做

- **鉴权。** 新端点没有身份认证，任何能访问 Worker HTTP 的人都能登记。已知，
  后续做。
- **Kubernetes。** 只做单节点。
- **升级流程。** 本次是不兼容变更，升级路径另行处理。
- **与 Ascend Docker Runtime 并存**（属 runtime 侧，见
  `neu_box_runtime/docs/runtime-hook.md`）。

## 已删除

| 删除 | 原来干什么 |
|---|---|
| `src/neu_box/runtime/gates.py` | gate token 的创建/等待/放行/丢弃 |
| `POST /sandbox/gate` 及 `/<token>/*` | 容器内向 Worker 报告的六个端点 |
| `POST /sandbox/container` | 按 docker inspect 查 PID 的旧登记入口 |
| `execution/docker.py` 里的 `_gate_argv` / `_start_container` / `_release_gate` | 包 ENTRYPOINT、SIGSTOP/SIGCONT |
| 客户端的 `gateclient` / `dockerargs` 里的 gate 部分 / `gate` 子命令 | 容器内握手（goClient 侧） |
