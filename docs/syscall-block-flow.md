# 设备沙盒：`open` 被 cgroup device BPF 拒绝的完整流程

## 场景

> 下文中的 `235` 仅是便于展示流程的示例。实际 Ascend major 由用户态从
> `/proc/devices` 的 `devdrv-cdev` 动态读取并写入 BPF map，不能假设固定为 235。

沙盒 A（cgroup `sandbox_box_a`）预留了设备 `235:1`（Ascend NPU 设备 1），
沙盒 B（cgroup `sandbox_box_b`）预留了设备 `235:0`（Ascend NPU 设备 0）。

> 本文中的 `235` 只是为了说明流程的占位值，不是配置值，也不保证
> 与任意节点当前的 major 相同。实现从 `/proc/devices` 动态解析
> `devdrv-cdev` 的 major，再写入 `devdrv_major` map。Worker 默认用
> `NEU_BOX_DEVICE_FILTER=davinci[0-9]+` 在 `/dev` 下发现 Ascend 计算设备；
> 因此本文用 `/dev/davinci0` 作为设备节点示例。

**沙盒 A 内的进程尝试访问 `/dev/davinci0`（在本文示例中为
major=235, minor=0），该设备不属于它，触发 BPF 拦截。**

---

## 一、架构总览

```
用户态进程（沙盒 A 内）
  │  open("/dev/davinci0", O_RDWR)
  ▼
内核 VFS 层
  │  openat/openat2 → do_sys_openat2() → do_filp_open()
  │  → path_openat() → do_open() → may_open() → inode_permission()
  ▼
cgroup device 权限检查
  │  devcgroup_inode_permission() → devcgroup_check_permission()
  │  → BPF_CGROUP_RUN_PROG_DEVICE_CGROUP
  │  → __cgroup_bpf_check_dev_permission()
  │  执行当前 cgroup 继承的 BPF_PROG_TYPE_CGROUP_DEVICE 程序
  ▼
eBPF 程序: device_reserve()  ◄── 挂载在 /sys/fs/cgroup（root cgroup）
  │
  │  查找 BPF maps:
  │    • reserved_devices:  key=(major, minor) → value=cgroup_id（精确/通配预留）
  │    • reserved_majors:   key=(cgroup_id, major) → value=1（major 级预留标记）
  │    • devdrv_major:      key=0 → value=devdrv-cdev 当前动态 major
  │
  │  判断：当前 cgroup 是否有权访问该设备？
  │
  ├── return 1 → 放行 → open() 继续执行
  └── return 0 → 阻拦 → open() 返回 -EPERM
```

---

## 二、BPF 程序 `device_reserve` 的决策流程

> 源码仓库路径：`native/sandbox/bpf/device_block.bpf.c`（发布包不携带源码）。

```
入口: device_reserve(struct bpf_cgroup_dev_ctx *ctx)

ctx 结构:
  ┌──────────────┬──────────┬──────────┐
  │ access_type  │  major   │  minor   │
  │   (u32)      │  (u32)   │  (u32)   │
  └──────────────┴──────────┴──────────┘
  access_type & 0xFFFF:
    BPF_DEVCG_DEV_BLOCK = 1  (块设备)
    BPF_DEVCG_DEV_CHAR  = 2  (字符设备)
```

### 决策图

```
                    ┌──────────────┐
                    │  BPF 程序入口 │
                    └──────┬───────┘
                           ▼
               ┌───────────────────────┐
               │ 是字符设备吗？          │
               │ dev_type == DEV_CHAR?  │
               └───────┬───────────────┘
                   N   │   Y
         ┌────────────┘   └────────────┐
         ▼                             ▼
   return 1                    ┌──────────────────────────┐
   (放行全部块设备)              │ 过滤特殊设备/无关 major   │
                               │ major==195 && minor==255 │
                               │ OR major∉{devdrv, 195}   │
                               └──────┬───────────────────┘
                                  Y   │   N (major=devdrv 或 195)
                           ┌──────────┘   └──────────┐
                           ▼                          ▼
                     return 1            ┌─────────────────────┐
                     (放行)              │ 归一 sandbox_cgid    │
                                         │ current + 绝对层级 1 │
                                         └──────────┬──────────┘
                                                    ▼
                                         ┌────────────────────────┐
                                         │ ① 精确查找 (235, 0)     │
                                         └───────────┬────────────┘
                                                     ▼
                                       命中 ── owner==sandbox_cgid? ──► 放行/拒绝
                                                     │ 未命中
                                                     ▼
                                         ┌────────────────────────┐
                                         │ ② 通配查找 (235, *)     │
                                         └───────────┬────────────┘
                                                     ▼
                                       命中 ── owner==sandbox_cgid? ──► 放行/拒绝
                                                     │ 未命中
                                                     ▼
                                         ┌────────────────────────┐
                                         │ ③ 查 reserved_majors   │
                                         │ key=(sandbox_cgid,235) │
                                         └───────────┬────────────┘
                                                     ▼
                                         命中则拒绝；未命中则放行
```

---

## 三、被拦截场景的具体追踪

用户态总是把 `sandbox_*` 直接创建在 cgroup v2 root 下，也就是绝对层级 1。
BPF 先读取 current cgroup ID，再取绝对层级 1 的 ancestor：直属进程得到自身
sandbox ID，Docker/systemd 创建的任意深度子 cgroup 得到同一个顶层 sandbox ID；
root 进程没有层级 1 ancestor 时才回退到 current ID。这样 map owner 不会因容器
子层级而失配。该 helper 要求 Linux 5.7 或更高版本。

### 场景：沙盒 A 内进程访问 `/dev/davinci0` (示例 235:0)

**前提条件：**
- 沙盒 A 的 cgroup_id = `0x1000001`，预留了 `235:1`
- 沙盒 B 的 cgroup_id = `0x2000001`，预留了 `235:0`
- BPF 程序已加载并挂载到 root cgroup

#### 沙盒 B 精确预留 `235:0` 的情况

reserved_devices map 中：
```
key={major:235, minor:0}  →  value=0x2000001  (沙盒 B 的 cgroup_id)
```

reserved_majors map 中：
```
key={cgid:0x2000001, major:235}  →  value=1
```

**拦截路径：**

| 步骤 | 操作 | 结果 |
|------|------|------|
| ① | 进程调用 `open("/dev/davinci0")` | 内核进入 VFS → device cgroup 权限检查 |
| ② | BPF 程序被调用，`ctx={type=DEV_CHAR, major=235, minor=0}` | — |
| ③ | 检查 `dev_type == DEV_CHAR` | ✅ 是字符设备，继续 |
| ④ | 检查特殊设备过滤 `major==195 && minor==255` | ❌ 不满足，继续 |
| ⑤ | 检查 `major∈{devdrv_major, 195}` | ✅ 示例中的 devdrv_major=235，继续 |
| ⑥ | 读取 current ID，并用 `bpf_get_current_ancestor_cgroup_id(1)` 归一 | `sandbox_cgid=0x1000001`（沙盒 A） |
| ⑦ | `bpf_map_lookup_elem(&reserved_devices, {235, 0})` | 命中！owner = `0x2000001`（沙盒 B） |
| ⑧ | `*owner (0x2000001) == sandbox_cgid (0x1000001)` | ❌ 不相等，这是别人的设备 |
| ⑨ | `return 0` | **拒绝访问** |

#### 沙盒 B 通配预留 `235:*` 的情况

这是独立于上面精确预留例子的另一组 map 状态：沙盒 B 通配预留整个
major 235，访问者沙盒 X（cgroup_id=`0x5000001`）没有 major 235 预留。
用户态不允许不同 owner 的 `235:*` 与任何 `235:<minor>` 同时存在。

reserved_devices map 中：
```
key={major:235, minor:0xFFFFFFFF}  →  value=0x2000001
```

reserved_majors map 中：
```
key={cgid:0x2000001, major:235}  →  value=1
```

**拦截路径：**

| 步骤 | 操作 | 结果 |
|------|------|------|
| ①-⑥ | （同上） | sandbox_cgid = `0x5000001`（沙盒 X） |
| ⑦ | `bpf_map_lookup_elem(&reserved_devices, {235, 0})` | **未命中**（map 里只有 `{235, 0xFFFFFFFF}`） |
| ⑧ | `bpf_map_lookup_elem(&reserved_devices, {235, 0xFFFFFFFF})` | 命中！owner = `0x2000001`（沙盒 B） |
| ⑨ | `*owner (0x2000001) == sandbox_cgid (0x5000001)` | ❌ 不相等，这是别人的通配预留 |
| ⑩ | `return 0` | **拒绝访问** |

如果访问者就是沙盒 B，步骤⑨的 owner 相等，程序返回 1；因此 `235:*`
会放行 owner 对该 major 下实际 minor 的访问，并拒绝其他 cgroup。

#### 沙盒 A 的同 major 自约束

`reserved_majors` 查的是 `(sandbox_cgid, major)`，回答“当前 sandbox 是否已经在这个
major 上预留过设备”。它在精确和通配查找都未命中后执行：有标记说明当前沙盒
正在越权访问同 major 下未分配给自己的 minor，因此拒绝。

**沙盒 A 预留 235:1，尝试访问 235:2（无人精确或通配预留）：**

| 步骤 | 操作 | 结果 |
|------|------|------|
| ①-⑥ | 同上 | sandbox_cgid = `0x1000001` |
| ⑦ | `lookup(reserved_devices, {235, 2})` | **未命中** |
| ⑧ | `lookup(reserved_devices, {235, 0xFFFFFFFF})` | **未命中** |
| ⑨ | `lookup(reserved_majors, {0x1000001, 235})` | **命中！** value=1（沙盒 A 自己在 major 235 上有预留） |
| ⑩ | `has_major && *has_major` | ✅ true |
| ⑪ | `return 0` | **拒绝！** 235:2 不在沙盒 A 的预留列表中 |

**没有预留 major 235 的沙盒 X 访问未被精确或通配预留的 235:2：**

| 步骤 | 操作 | 结果 |
|------|------|------|
| ⑦ | `lookup(reserved_devices, {235, 2})` | **未命中** |
| ⑧ | `lookup(reserved_devices, {235, 0xFFFFFFFF})` | **未命中** |
| ⑨ | `lookup(reserved_majors, {0x5000001, 235})` | **未命中**（沙盒 X 没有预留 major 235） |
| ⑩ | `return 1` | **放行！**（这是一个没有任何沙盒预留的"自由"设备） |

决策顺序固定为：**精确预留 → major 通配预留 → 当前 cgroup 的 major
自约束 → 放行**。用户态在写 map 前拒绝与不同 owner 已有条目
冲突的 exact/wildcard 请求，同一 owner 的重复写入则按幂等操作处理。

这里保持的是“全局设备预留”语义，不是每个 sandbox 的完整 allowlist：只有已经
写入 `reserved_devices` 的设备会对其他 sandbox 拒绝访问；某个 sandbox 在一个
major 上有预留时，`reserved_majors` 才会限制它访问该 major 下未分配的 minor。
完全未预留的设备仍走最终放行。`npu-smi`/`nvidia-smi` 报出的外部 busy 状态只参与
用户态调度，不会自动写进 BPF map。

---

## 四、内核侧完整调用链

下图是对“打开一个已存在的设备节点”有关的关键路径，省略了无关的
path-walk 细节。Linux 5.10 与 6.6 的 `inode_permission()` 签名和 BPF
运行器的内部返回值传递略有不同，但设备权限检查的主链一致：

```
用户态: open("/dev/davinci0", O_RDWR)
  │
  ▼
fs/open.c: openat/openat2
  │
  ▼
do_sys_openat2() → do_filp_open() → path_openat() → do_open()
  │
  ▼
fs/namei.c: may_open()
  │
  ▼
fs/namei.c: inode_permission(..., inode, MAY_OPEN | acc_mode)
  │
  ▼
include/linux/device_cgroup.h: devcgroup_inode_permission(inode, mask)
  │  确认 inode 是字符/块设备
  │  从 inode 取 imajor()/iminor()，从 mask 生成 READ/WRITE access
  │
  ▼
security/device_cgroup.c: devcgroup_check_permission(type, major, minor, access)
  │
  ▼
BPF_CGROUP_RUN_PROG_DEVICE_CGROUP(...)
  │
  ▼
kernel/bpf/cgroup.c: __cgroup_bpf_check_dev_permission(...)
  │  构造 struct bpf_cgroup_dev_ctx
  │  运行当前 cgroup 的 effective CGROUP_DEVICE 程序数组
  │
  ▼
device_reserve(ctx)
  │
  ├── return 1 (ALLOW) → may_open() 继续，之后才进入 vfs_open()
  └── return 0 (DENY)  → 权限链返回 -EPERM，vfs_open() 不会执行
```

因此，cgroup device BPF 检查不是由 `security_file_open()` 调用；
`security_file_open()` 是后续打开文件时的 LSM hook，不在这条 device cgroup
路径上。Linux 5.10 中，BPF 适配层先把程序的 `0` 转成“拒绝”，
`devcgroup_check_permission()` 再转成 `-EPERM`；Linux 6.6 的 BPF 程序
数组运行器直接传回 `-EPERM`。两者对用户态的结果相同。

上述链路可以在上游源码中交叉核对：

- Linux 5.10：[`may_open()`](https://github.com/torvalds/linux/blob/v5.10/fs/namei.c#L2840-L2877)、[`inode_permission()`](https://github.com/torvalds/linux/blob/v5.10/fs/namei.c#L2932-L2983)、[`devcgroup_inode_permission()`](https://github.com/torvalds/linux/blob/v5.10/include/linux/device_cgroup.h#L14-L38)、[`devcgroup_check_permission()`](https://github.com/torvalds/linux/blob/v5.10/security/device_cgroup.c#L833-L850)、[`__cgroup_bpf_check_dev_permission()`](https://github.com/torvalds/linux/blob/v5.10/kernel/bpf/cgroup.c#L1125-L1143)。
- Linux 6.6：[`may_open()`](https://github.com/torvalds/linux/blob/v6.6/fs/namei.c#L3232-L3270)、[`inode_permission()`](https://github.com/torvalds/linux/blob/v6.6/fs/namei.c#L3048-L3102)、[`devcgroup_inode_permission()`](https://github.com/torvalds/linux/blob/v6.6/include/linux/device_cgroup.h#L14-L38)、[`devcgroup_check_permission()`](https://github.com/torvalds/linux/blob/v6.6/security/device_cgroup.c#L858-L875)、[`__cgroup_bpf_check_dev_permission()`](https://github.com/torvalds/linux/blob/v6.6/kernel/bpf/cgroup.c#L1521-L1539)。

### 返回值对用户态的影响

```
BPF 返回 0（DENY）
  │
  ▼
__cgroup_bpf_check_dev_permission() / devcgroup_check_permission()
将拒绝转换为 -EPERM
  │
  ▼
inode_permission() → may_open() → do_open() 失败
  │
  ... 层层返回 ...
  │
  ▼
用户态: open() 返回 -1, errno = EPERM (Operation not permitted)
```

---

## 五、用户态可观测的现象

```bash
# 沙盒 A 内进程尝试访问不属于它的设备
$ cat /dev/davinci0
cat: /dev/davinci0: Operation not permitted

# strace 跟踪
$ strace -e trace=open,openat,openat2 cat /dev/davinci0
openat(AT_FDCWD, "/dev/davinci0", O_RDONLY) = -1 EPERM (Operation not permitted)

# 查看 cgroup、设备预留 map 和进程列表
$ sudo /usr/libexec/neu-box/neu-box-sandbox status box_a
```

当前生产 BPF 程序没有启用 `bpf_printk`，因此 `dmesg` 不会出现逐次
ALLOW/DENY 日志。用 `strace` 确认返回给任务的 `EPERM`，再用
`neu-box-sandbox status` 核对当时的 owner map；不要以 `dmesg` 没有日志
作为“没有进入 BPF”的证据。

---

## 六、两表协同的拦截机制总结

```
┌─────────────────────────────────────────────────────────────┐
│                  reserved_devices                            │
│  key=(major, minor)  →  value=cgroup_id                     │
│                                                              │
│  记录精确设备或 major:* 通配预留的 owner                       │
│  ┌──────────┬───────────┬──────────────┐                    │
│  │ (235, 0) │ 0x2000001 │ 沙盒 B 独占  │                    │
│  │ (235, 1) │ 0x1000001 │ 沙盒 A 独占  │                    │
│  │ (195, 0) │ 0x3000001 │ 沙盒 C 独占  │                    │
│  └──────────┴───────────┴──────────────┘                    │
└─────────────────────────────────────────────────────────────┘

通配替代状态: (235, 0xFFFFFFFF) → owner；不会与不同 owner 的
同-major 精确条目并存。

┌─────────────────────────────────────────────────────────────┐
│                  reserved_majors                              │
│  key=(cgroup_id, major)  →  value=1 (标记位)                │
│                                                              │
│  自声明：这个 cgroup 在这个 major 上至少预留了一个设备        │
│  ┌──────────────────────┬───────┬────────────────┐          │
│  │ (0x1000001, 235)     │   1   │ 沙盒 A mj=235  │          │
│  │ (0x2000001, 235)     │   1   │ 沙盒 B mj=235  │          │
│  │ (0x3000001, 195)     │   1   │ 沙盒 C mj=195  │          │
│  └──────────────────────┴───────┴────────────────┘          │
└─────────────────────────────────────────────────────────────┘

拦截决策矩阵（对于沙盒 A，cgid=0x1000001）：

  访问目标          reserved_devices 命中?    reserved_majors 命中?    结果
  ─────────        ──────────────────────    ─────────────────────    ────
  235:1 (我的)      ✅ owner = 0x1000001     —                        ✅ 放行
  235:0 (别人的)    ✅ owner = 0x2000001     —                        ❌ DENY
  235:2 (无人预留)  ❌                       ✅ (自己,mj235)=1        ❌ DENY
  195:0 (别人的)    ✅ owner = 0x3000001     —                        ❌ DENY
  195:255 (控制卡)  — (显式共享)             —                        ✅ 放行
  sda (块设备)      — (不是 char device)     —                        ✅ 放行
  /dev/null (1:3)   — (mj=1 不在过滤范围)    —                        ✅ 放行
```

> 💡 **核心思想**：BPF 只拦截 `major ∈ {devdrv_major, 195}` 的字符设备（Ascend NPU 和 NVIDIA GPU），其中 `devdrv_major` 由用户态动态配置，其他设备全部放行。对于这些目标 major，先检查精确 owner，再检查 `major:*` owner；两者都没有时，若当前 cgroup 在该 major 上有预留声明但访问的 minor 不在自己的精确列表中，则按自约束拒绝。
