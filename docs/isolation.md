# 设备隔离的实现原理（维护者视角）

一句话：**卡给谁，最终由"这张卡被哪个沙盒预留 + 谁在这个沙盒的授权名单里"决定，
而这个决定在三个地方各表达一次 —— 我们的预留表、容器的 mnt ns 委托、驱动的
UDA 设备表。** 三处必须同时对，隔离才成立；任何一处被绕过去，前面两处都白做。

这份文档讲的是"为什么现在这么做"，不是操作手册。端点字段以
[`worker-api.md`](worker-api.md) 为准，登记流程的 Worker 侧细节见
[`container-registration.md`](container-registration.md)，runtime 侧见
`neu_box_runtime/docs/runtime-hook.md`。

## 三层，各管一段

| 层 | 载体 | 判据 | 代码 |
|---|---|---|---|
| ① 沙盒预留 | BPF map `reserved_devices[(major,minor)] = cgroup_id` | 宿主进程看自己 cgroup 有没有预留 | `native/sandbox/bpf/device_block.bpf.c`、`native/sandbox/src/reservation.cpp` |
| ② 容器委托 | BPF map `container_owner[mnt_ns_inum] = sandbox_cgroup_id` | 容器进程先认出"我是容器"，再查委托方沙盒有没有这张卡 | 同上 + `SbxManager.bind_container` |
| ③ 驱动设备表 | 驱动按 mnt ns 缓存的 UDA ns node | Ascend 驱动自己的 admin 判定 + 该 ns "占"到了哪几张卡 | `gitcode.com/cann/driver`：`uda_access.c` / `uda_fops.c` / `ka_system.c` |

**①②是同一张 BPF 程序的容器分支和宿主分支**，③不在我们手里 —— 这就是本文要讲
清楚的地方：它既是我们最容易被绕过的一环，也是我们**无法**主动清理的一环。

## ① 预留：写在 cgroup 上

`neubox acquire`（`POST /sandbox/acquire`）建一个沙盒 cgroup，把设备写进
`reserved_devices`：`(major, minor) → 沙盒 cgroup id`，同时写
`reserved_majors[(cgid, major)]` 表示"这个沙盒在该 major 上受管"。借出去的
shell 被直接写进沙盒 cgroup（`cgroup.procs`，没有嵌套层级，所以判定只做精确
匹配、不向上找祖先）。

宿主进程的判定与历史行为逐字一致：

1. 精确命中预留且预留者是自己的 cgroup → 放行；
2. 精确命中但预留者是别人 → 拒绝；
3. 该 major 上有自己的预留、这张卡却没拿到 → 拒绝；
4. 其余（未受管的 major、其他设备）→ 放行。

所以**宿主上"没人预留的卡"是所有人可见的**，被预留的卡只有预留者能用。这也是
为什么需要 ②：容器不能沿用这套规则 —— 它必须一律拒绝没被委托的卡，包括空闲卡。

## ② 委托：写在 mount namespace 上

容器不进沙盒 cgroup。它靠 OCI runtime hook 在 `runc create` 阶段把自己"挂"到沙盒
名下：

```
neubox docker run --annotation sandbox_cgroup=<沙盒名>
        │
   dockerd → containerd → neu-box-runtime（注入 createRuntime hook）
                              │
                          真 runc 建 namespace
                              │
                        neu-box-hook（读 stdin 的 OCI state）
                              │  POST /container/register
                              │  {container_id, host_pid, sandbox_cgroup}
                              ▼
                     Worker：/proc 推 mnt ns inum + init start_time
                              │  bind-container <沙盒> <mnt_ns_inum>
                              ▼
        container_owner[mnt_ns_inum] = 沙盒 cgroup id   （+ DB 落账 + 钉 mnt ns fd / pidfd）
```

容器进程的判定（`device_block.bpf.c` 的容器分支）：

1. 当前 mnt ns 与宿主不同 → 是容器；
2. `container_owner[mnt_ns]` 查不到委托方 → **一律拒绝**（fail-closed）；
3. 查到委托方后，只有 `reserved_devices[(major,minor)] == 委托方 cgroup id` 才放行；
4. **空闲卡也拒绝** —— 容器分支不看"设备空不空"，只看委托方有没有预留。

这套 key 与驱动同宽：用户态 `stat("/proc/<pid>/ns/mnt").st_ino` 就是 BPF 里 CO-RE
读到的 `mnt_namespace.ns.inum`。所以**委托是"按 mount namespace"给出去的**，不是
按进程、不是按容器 ID。

三条推论，后面反复用到：

* 容器里**任何**进程（包括 `docker exec` 起的）都自动继承这份委托 —— 同一个 ns；
* 撤销委托（`unbind`）只影响**之后**的 open：已经打开的 fd 不受影响，所以撤授权
  之后必须把进程真的收掉（见"释放路径"）；
* 容器超时/崩溃不会留下"半张委托"：委托表要么有这条记录（= 沙盒授权有效），要么没有。

## ③ 驱动：第二道门，而且它只认自己的账

Ascend 驱动给每个 mount namespace 建一张 UDA 设备表，**按 mnt ns 缓存**，表里
有几张卡就是容器里 `torch.npu.device_count()` / `npu-smi` 能看见几张。它和我们
的 eBPF 是两条独立的路：eBPF 拦的是 `open("/dev/davinciN")`，而真实负载（torch、
HCCL）主要走 `/dev/davinci_manager` + UDA ioctl，**不 open 那些节点**。

### admin 分支：必须让容器掉出去

```c
uda_is_admin_task() = cred->user_ns == &init_user_ns
                      && cap_effective ⊇ ka_system_get_privileged_kernel_cap()
uda_cur_is_admin()  = uda_cur_is_host() || uda_current_is_admin()
```

* 掩码是 bits 0..37（`CAP_CHOWN`…`CAP_AUDIT_READ`，≥6.3 内核）；
* **宿主 mnt ns 里的进程永远算 admin**（`uda_cur_is_host()`），所以宿主 shell 从不
  "占用"设备，它的表是 `uda_devcgroup_permission_allow` 里那次内核 `open` 被 ①
  挡出来的结果 —— 宿主这一侧的隔离靠 ①，不靠 ③；
* 被判成 admin 的**容器**会走 `max_num = UDA_MAX_PHY_DEV_NUM` 建出**全量**表，
  并且跳过"占用"这一步：`docker run --privileged` / `--cap-add=ALL` 就是这个坑，
  真机复现过申请 2 张卡的容器里 `device_count() == 8`。

对策在 runtime 侧：`neu-box-runtime` 的能力位守卫（默认
`NEU_BOX_CAP_GUARD=drop`）从 bounding/permitted/effective/ambient 里剪掉
`CAP_AUDIT_READ`，容器就掉出 admin 超集，其余能力位一个不动。这条对**所有**
经过 wrapper 的容器生效（不只是沙盒容器），create 和 `docker exec` 两条路径
各自剪一次 —— exec 的能力位是 docker 按容器 HostConfig 现算的，跟被我们改过的
bundle 无关。

### 非 admin 分支：表 = "占到的卡"

非 admin 的实现细节决定了我们的用法：

* `uda_access_open()`：`if (!uda_cur_is_admin())` 才把这张卡"占用"到当前 mnt ns
  （`uda_occupy_dev_by_ns`），占用的粒度就是 mnt ns；
* 建表时（`_uda_setup_ns_node`）非 admin 会拿用户态报的期望卡数跟实际占到的数量
  比，对不上直接 `-EBUSY` 并把刚建的节点拆掉；
* 一张卡同一时刻只能被一个非 admin ns 占用，**除非这张卡开了 share**
  （`uda_set_dev_share`，走 manager ioctl / DCMI 的 device-share）。share 是机器
  状态：设备 init/热复位时会被复位成 unshare，而且已经有非 admin ns 持卡时设不
  上去（`-EBUSY`）。这台机器上 8 张卡现在是 shared（装机/驱动重载后要确认，
  用 `/proc/uda/udevice` 看有没有 `shared dev used by ns id:` 行）；
* 表是**永久缓存**的：驱动没有"删表"的接口（`uda_uninit_ns_node_dev` /
  `uda_destroy_ns_node` 都是 static，`/proc/uda/*` 全 0400 只读，manager ioctl
  里也没有对应 cmd）。能让它失效的只有：那个 mnt ns 死掉（= 里面没有进程）之后
  被驱动的 idle 回收捡走 —— 读一次 `/proc/uda/namespace_node` 就会触发
  `uda_recycle_idle_ns_node_immediately()`，下一次 `uda_setup_ns_node` 也会顺手
  扫一遍。

**这就是"登记必须卡在 ENTRYPOINT 之前"的全部理由**：容器里第一个 NPU 进程一跑，
表就定下来了；登记晚了会建出一张空表并按 mnt ns 永久复用，事后补登记也修不回来。
所以 hook 失败只能让容器起不来，不能降级放行。

## 生命周期逐条对照

| 事件 | 发生什么 | 隔离结论 |
|---|---|---|
| 正常使用 | 容器 init（或 exec 出来的进程）在 ENTRYPOINT 前后 open 自己沙盒的卡 → ②放行 → 驱动把这几张卡记进这个 ns 的表 | 只能看到自己沙盒的卡 |
| 容器自己退出 | pidfd 事件唤醒收尸线程，撤登记、删记录；**沙盒的设备不释放** | 死 ns 的表没人能用 |
| `POST /sandbox/release` | 撤授权 → `docker stop` → 等容器真的退出 → 删记录/放 pin → native destroy 清 cgroup 与预留 | 容器停着（**不删**，可写层留着），卡回池 |
| release 失败（容器不退） | destroy 返回失败、沙盒保留 `DESTROYING` 等收尸重试，**卡不放回** | 宁可少发一张，不发一张还在被用的 |
| `docker stop` 后 `docker start` | start 重走整条 create → hook 再登记一次；annotation 还是**沙盒名**，精确匹配 | 沙盒还在 → 幂等登记、照旧带卡；沙盒没了 → 404 → **放行但零卡**（见下） |
| `docker exec` | 不走 hook（不是 create），复用同一个 mnt ns 的委托与同一张表 | 它就是当初登记成功的那份授权，不多不少 |
| 容器没带 annotation | wrapper 不注入 hook，容器照常起 | 没有委托 → open 全拒、表为空（fail-closed） |
| **`docker start` 一个沙盒已释放的老容器** | Worker 明确回答"没有这个沙盒"（404/409）→ hook **无授权放行** | 容器能起来（可写层还在），但一张卡都拿不到；要卡得重新 acquire 并重建容器 |
| 其它任何登记失败（连不上 Worker / 超时 / 5xx / 身份冲突 409） | hook 退非 0 → `runc create` 失败 | 绝不降级放行：拿不到授权答案时，维护窗口里 BPF 可能是拆掉的，放行等于把全部卡送出去 |
| annotation 指向不存在的沙盒 | hook 404 → 退非 0 → `runc create` 失败 | 容器起不来 |
| worker 重启（崩溃后拉起） | 排队任务重新入队；**running 任务标 failed，任务沙盒连 cgroup 一起清**；acquire 沙盒保留；**名下还活着的容器一律停掉（不删）+ 撤绑定** | 重启不续授权：容器要 `docker start` 重新登记，任务要重新提交 |
| `--cap-add=ALL` / `--privileged` | runtime 剪掉 `CAP_AUDIT_READ` → 非 admin → 走"占到的卡" | 表 = 沙盒预留的卡 |

## 不变量（改代码时要守住的）

1. **容器分支不认 cgroup 位置、不认"设备空闲"**：只有委托方预留的卡才放行。
2. **委托 key 是 mnt ns inum，且必须钉住 mnt ns fd**：不钉的话 inum 会被复用，
   后来的容器白捡一份授权。
3. **登记必须与沙盒状态检查在同一个 `lifecycle_lock` 临界区里**（见
   `container-registration.md`），否则 destroy 能把授权从两次操作之间抽走。
4. **释放沙盒 = 真的把容器停掉（进程退光），但不要删它**：撤授权只挡新 open，
   进程还在就等于没撤；删掉又会连可写层一起毁掉，用户还要 commit / cp 呢。
5. **容器里第一个 NPU 进程之前必须有委托** —— 这是 hook 存在的唯一理由。
   唯一的例外是"授权的否定答案"（沙盒不存在 / 正在销毁）：那时放行是安全的，
   因为闸门还在、容器拿不到任何卡；"拿不到答案"（连不上、超时、5xx）不算例外。
6. **容器不能是 admin**：能力位守卫是 ③ 唯一能被我们控制的那一半。
7. **重启不续授权**：容器退出监听（mnt ns fd + pidfd）是内存态，重启后全丢；
   驱动那张按 mnt ns 缓存的表却还在。所以启动时把还活着的登记容器停掉、撤绑定
   （`SbxManager.retire_containers_on_startup`），任务标 failed 并清掉它的沙盒。
8. **授权只能来自显式声明，且只借属于你的那一份**：`docker run` 靠它那一刻写下的
   annotation，`docker start` 靠一张一次性借条（键 `(container_id, 属主)`，属主三段
   必须一致）。容器已经绑在某个沙盒上时，重复登记以**既有绑定**为准，不跟着这次
   报上来的沙盒改 —— 否则一次 `docker exec` 就能把运行中容器的授权搬走。

## 已知边界（不是 bug，是当前口径）

* `--runtime=runc` 完全绕过本仓库这一套（wrapper 没被调用）；要用这套隔离就必须
  用默认 runtime。
* 驱动那张表**不会**在容器退出时同步清掉；残留的死 ns 行只能等 idle 回收，读
  `/proc/uda/namespace_node` 可以催。残留的行本身无害（ns 里没有进程），但会
  在 `/proc/uda/udevice` 的 share 列表里留下 `128` 之类的占位。
* share 关闭时"一张卡只能被一个非 admin ns 占用"；宿主与容器共卡不受影响（宿主
  不占用），但**两个容器共卡**必须开着 share。
* `acquire` 的沙盒名是 `sbx_<owner>_<pid>`，pid 复用后同名重建有一个极窄窗口：
  老容器的 annotation 可能正好匹配上别人的新沙盒。命令任务的沙盒名带 uuid，
  只有 acquire 这条没有，后续考虑统一。
* 鉴权还没做（见 `container-registration.md`）。

## 自己验一遍

真机验收里有对应的用例，一条一条对着看即可（`tests/deployment/`，文件顺序在
`conftest.py` 的 `_FILE_ORDER`）：

| 想看什么 | 用例 |
|---|---|
| 带 annotation → 登记 → 容器里能开自己沙盒的卡 | 33 |
| 不带 annotation → 一张卡都拿不到 | 34 |
| annotation 指向不存在的沙盒 → 容器起得来但零卡（35 验用户态可见性，83 验驱动表） | 35、83 |
| 容器退出只注销登记、不释放沙盒设备 | 38 |
| 容器重启后重复登记幂等 | 39 |
| release → 名下容器被一并停掉、不删（含借出的终端不被误杀） | 42 |
| 崩溃重启：占卡任务变 failed、任务沙盒被清、卡回池 | 81 |
| 崩溃重启：acquire 沙盒保留，但名下容器被停掉、`docker start` 能重新登记回来 | 82 |
| 已登记容器的 UDA 表只有自己沙盒那几张 | 61 |
| 未登记容器的 UDA 表为空 | 62 |
| `--cap-add=ALL` 容器仍非 admin（init 与 exec 的 CapEff 都剪了位） | 64 |
| `docker exec` 起的进程只借到这一份授权 | 71 |
| release 之后容器停着、驱动表回收、同一张卡干净地交给下一家 | 72 |

真 `neubox` 走一遍那条经典路径（`test_client_docker.py`）：

| 想看什么 | 用例 |
|---|---|
| `neubox docker run` 注入 annotation → 登记到自己那个沙盒 | 67 |
| 不在沙盒里的 `neubox docker run` 拒绝启动 | 68 |
| 容器里只开得到自己沙盒的卡（空闲卡也不行） | 69 |
| `neubox release` 停掉 client 起的容器（不删） | 70 |
| 沙盒还在时 `docker stop` → `docker start`：重新登记、卡照旧能用 | 73 |
| stop 之后再 release：容器留着（可写层不丢），start 起得来但零卡 | 74 |
| 停着的老容器不挡路：start 起来也零卡，同一张卡照常交给下一个沙盒 | 75 |
| 跨 shell：`neubox docker start` 把**当前**沙盒借给老容器，同一个容器（annotation 没改）重新拿到卡 | 84 |
| 不在沙盒里的 `neubox docker start`：容器照样起来，但零卡且命令行明说 | 85 |

调度的队列语义（`test_scheduling.py`）：

| 想看什么 | 用例 |
|---|---|
| 不占卡的任务不被"等卡的队首"挡住 | 76 |
| 高优先级要 2 张、只有 1 张空：整单排队、不预占空卡 | 77 |
| 取消排队条目之后 position 连续、顺序不变 | 78 |
| 同优先级 8 条任务严格 FIFO | 79 |
| 同优先级下 task 与 acquire 交错也按提交顺序 | 80 |

手工看现场三件套：

```bash
sudo cat /proc/uda/udevice            # 每张卡：shared dev used by ns id: ... 或 has used by container, ns id N
sudo cat /proc/uda/namespace_node    # 一个 mnt ns 一行：root_tgid / dev_num / udev list（读它本身会催回收）
sudo bpftool map dump name container_owner   # 我们的委托表：mnt_ns_inum → 沙盒 cgroup id
```
