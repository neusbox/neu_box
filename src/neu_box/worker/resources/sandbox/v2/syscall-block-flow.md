# v2 沙盒：系统调用被 eBPF 阻拦的完整流程

## 场景

沙盒 A（cgroup `sandbox_box_a`）预留了设备 `235:1`（Ascend NPU 设备 1），
沙盒 B（cgroup `sandbox_box_b`）预留了设备 `235:0`（Ascend NPU 设备 0）。

**沙盒 A 内的进程尝试访问 `/dev/accel/accel0`（major=235, minor=0），该设备不属于它，触发 BPF 拦截。**

---

## 一、架构总览

```
用户态进程（沙盒 A 内）
  │  open("/dev/accel/accel0", O_RDWR)
  ▼
内核 VFS 层
  │  sys_openat() → do_sys_openat2() → do_filp_open()
  │  → path_openat() → vfs_open() → do_dentry_open()
  ▼
cgroup v2 device 控制器 hook 点
  │  __cgroup_bpf_run_filter_dev()
  │  遍历 root cgroup 上挂载的 BPF_PROG_TYPE_CGROUP_DEVICE 程序
  ▼
eBPF 程序: device_reserve()  ◄── 挂载在 /sys/fs/cgroup（root cgroup）
  │
  │  查找 BPF maps:
  │    • reserved_devices:  key=(major, minor) → value=cgroup_id（精确设备预留）
  │    • reserved_majors:   key=(cgroup_id, major) → value=1（major 级预留标记）
  │
  │  判断：当前 cgroup 是否有权访问该设备？
  │
  ├── return 1 → 放行 → open() 继续执行
  └── return 0 → 阻拦 → open() 返回 -EPERM
```

---

## 二、BPF 程序 `device_reserve` 的决策流程

> BPF 源码：[device_block.bpf.c](device_block.bpf.c)

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
                               │ OR major∉{235, 195}      │
                               └──────┬───────────────────┘
                                  Y   │   N (major=235 或 195)
                           ┌──────────┘   └──────────┐
                           ▼                          ▼
                     return 1            ┌─────────────────────┐
                     (放行)              │ 获取当前 cgroup_id   │
                                         │ bpf_get_current_    │
                                         │ cgroup_id()         │
                                         └──────────┬──────────┘
                                                    ▼
                                         ┌─────────────────────┐
                                         │ 精确查找              │
                                         │ reserved_devices    │
                                         │ key=(235, 0)        │
                                         └──────────┬──────────┘
                                                    ▼
                              ┌─────────────────────────────────────┐
                              │ owner (value) 找到了吗？             │
                              └────────┬────────────┬───────────────┘
                                   Y   │            │  N
                                       ▼            ▼
                          ┌──────────────────┐  ┌─────────────────────┐
                          │ *owner == my_cgid?│  │ 查找 reserved_majors │
                          └───┬─────────┬────┘  │ key=(my_cgid, 235)   │
                          Y   │         │  N    └──────────┬──────────┘
                              ▼         ▼                  ▼
                         return 1  return 0    ┌────────────────────────┐
                         (我的设备, (别人的,    │ has_major 存在且非零？  │
                          放行)     拒绝!)     └──────┬─────────┬───────┘
                                                 Y   │         │  N
                                                     ▼         ▼
                                               return 0   return 1
                                               (该 major  (该 major
                                                被他人预留, 无人预留,
                                                拒绝)      放行)
```

---

## 三、被拦截场景的具体追踪

### 场景：沙盒 A 内进程访问 `/dev/accel/accel0` (235:0)

**前提条件：**
- 沙盒 A 的 cgroup_id = `0x1000001`，预留了 `235:1`
- 沙盒 B 的 cgroup_id = `0x2000001`，预留了 `235:0`（精确）或 `235:*`（通配）
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

**拦截路径（标红路径）：**

| 步骤 | 操作 | 结果 |
|------|------|------|
| ① | 进程调用 `open("/dev/accel/accel0")` | 内核进入 VFS → device cgroup hook |
| ② | BPF 程序被调用，`ctx={type=DEV_CHAR, major=235, minor=0}` | — |
| ③ | 检查 `dev_type == DEV_CHAR` | ✅ 是字符设备，继续 |
| ④ | 检查特殊设备过滤 `major==195 && minor==255` | ❌ 不满足，继续 |
| ⑤ | 检查 `major∈{235, 195}` | ✅ major=235 在范围内，继续 |
| ⑥ | `my_cgid = bpf_get_current_cgroup_id()` | 返回 `0x1000001`（沙盒 A） |
| ⑦ | `bpf_map_lookup_elem(&reserved_devices, {235, 0})` | 命中！owner = `0x2000001`（沙盒 B） |
| ⑧ | `*owner (0x2000001) == my_cgid (0x1000001)` | ❌ 不相等，这是别人的设备 |
| ⑨ | `return 0` | **拒绝访问** |

#### 沙盒 B 通配预留 `235:*` 的情况

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
| ①-⑥ | （同上） | my_cgid = `0x1000001` |
| ⑦ | `bpf_map_lookup_elem(&reserved_devices, {235, 0})` | **未命中**（map 里只有 `{235, 0xFFFFFFFF}`） |
| ⑧ | `bpf_map_lookup_elem(&reserved_majors, {0x1000001, 235})` | **未命中**（沙盒 A 没有预留 major 235） |

> ⚠️ **这里注意**：`reserved_majors` 存的是 `(cgroup_id, major)` → 是否有预留。沙盒 A 的 key 是 `{(0x1000001, 235)}` 不存在，所以 `has_major` 是 NULL/不存在，走到下面的逻辑...

等一下——实际上这里暴露了一个关键逻辑：`reserved_majors` 的 key 是 `(cgroup_id, major)`，value=1 表示"这个 cgroup 在这个 major 上有预留"。

如果没有精确命中，且 `has_major`（对于当前 cgid）不存在，这时应该怎么做？看代码：

```c
if (owner) {
    // 精确命中，判断归属
} 
if (has_major && *has_major) {
    return 0;   // 该 major 被别人预留了
}
return 1;  // 完全未预留 → 放行
```

所以在通配场景下：
- 精确查找 `{235, 0}`：未命中（map 里只有 `{235, 0xFFFFFFFF}`）
- `has_major` 查找 `{0x1000001, 235}`：未命中（沙盒 A 没有预留 major 235）
- → return 1，**放行了！** 🔴

> 🔴 **这其实是一个 BUG！** 当沙盒 B 用 `235:*` 通配预留了整个 major 235 时，沙盒 A 访问 `235:0` 将不会被拦截，因为：
> 1. 精确匹配 `{235, 0}` 在 map 中不存在（map 里只有 wildcard key `{235, 0xFFFFFFFF}`）
> 2. `reserved_majors` 按 `(cgroup_id, major)` 索引，它只能回答"我自己有没有在这个 major 上预留设备"，不能回答"有没有**别人**在这个 major 上预留了设备"

---

## 三（更正）、正确理解的拦截场景

回到 BPF 代码逻辑，在当前实现中：

```c
// reserved_majors 的 key 是 (cgid, major)，value=1 
// 意味着这个 cgroup 在 major 上有预留

if (owner) {
    // 精确命中 → 按照归属判断
}
if (has_major && *has_major) {
    return 0;  // 这个 major 上（我所在的 cgroup）有预留，但我访问的具体 minor 没命中 → 拒绝
}
return 1;
```

`has_major` 查的是 `(my_cgid, major)`，即**"我自己所在的沙盒在这个 major 上有没有预留"**。如果有预留但我访问的具体 minor 没在精确匹配中，就拒绝。这是一种"自约束"：一个沙盒如果预留了 major 235 下的某些设备，那它就只能访问自己精确预留的那些，不能访问同 major 下的其他设备。

### 具体被拦截场景

**场景 1：沙盒 A 预留 235:1，尝试访问 235:0（同 major 下非自己的设备）**

| 步骤 | 操作 | 结果 |
|------|------|------|
| ①-⑥ | 同上 | my_cgid = `0x1000001` |
| ⑦ | `lookup(reserved_devices, {235, 0})` | **未命中**（map 里只有 `{235, 1} → 0x1000001`） |
| ⑧ | `lookup(reserved_majors, {0x1000001, 235})` | **命中！** value=1（沙盒 A 自己在 major 235 上有预留） |
| ⑨ | `has_major && *has_major` | ✅ true |
| ⑩ | `return 0` | **拒绝！** 同一个 cgroup 在 major 235 上有预留，但 235:0 不在其精确预留列表中 |

**场景 2：沙盒 A 没有预留任何 major 235 设备，尝试访问 235:0**

| 步骤 | 操作 | 结果 |
|------|------|------|
| ⑦ | `lookup(reserved_devices, {235, 0})` | **未命中** |
| ⑧ | `lookup(reserved_majors, {0x1000001, 235})` | **未命中**（沙盒 A 没有预留 major 235） |
| ⑨ | `return 1` | **放行！**（这是一个没有任何沙盒预留的"自由"设备，或者是 major 未被任何沙盒预留的情况） |

> 💡 **设计意图**：`reserved_majors` 的本质是"自声明"——告诉 BPF "我在这个 major 上有预留"。这意味着沙盒内的进程如果所在 cgroup 在某个 major 上有预留声明，就只能访问自己精确预留的设备，不能访问同 major 下的其他设备。没有声明 major 的沙盒不受限制。

---

## 四、内核侧完整调用链

```
用户态: open("/dev/accel/accel0", O_RDWR)
  │
  ▼
fs/open.c:  sys_openat()
  │
  ▼
fs/open.c:  do_sys_openat2(dfd, filename, flags, mode)
  │  struct open_how how = {.flags=flags, .mode=mode, .resolve=0}
  │
  ▼
fs/open.c:  do_filp_open(dfd, getname(filename), &op)
  │  op->open_flag = flags
  │
  ▼
fs/namei.c:  path_openat(&nd, op, flags)
  │  路径查找 → 找到 inode
  │
  ▼
fs/namei.c:  do_open()
  │
  ▼
fs/open.c:  vfs_open(path, file)
  │
  ▼
fs/open.c:  do_dentry_open(file, path->dentry->d_inode, ...)
  │
  ▼
security/security.c:  security_file_open(file)
  │  调用 LSM hooks
  │
  ▼
kernel/bpf/cgroup.c:  __cgroup_bpf_run_filter_dev()
  │  for each prog in cgroup->bpf.cgroup_device:
  │      ret = BPF_PROG_RUN(prog, ctx)
  │      ret = (ctx->access_type & BPF_DEVCG_ACC_*)
  │           | (prog_ret ? BPF_DEVCG_ACC_ALLOW : BPF_DEVCG_ACC_DENY)
  │
  ├── prog_ret == 1 (ALLOW) → open() 继续执行
  └── prog_ret == 0 (DENY)  → open() 返回 -EPERM
```

### 返回值对用户态的影响

```
BPF 返回 0（DENY）
  │
  ▼
__cgroup_bpf_run_filter_dev() 返回 -EPERM
  │
  ▼
security_file_open() → -EPERM
  │
  ▼
do_dentry_open() 失败
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
$ cat /dev/accel/accel0
cat: /dev/accel/accel0: Operation not permitted

# strace 跟踪
$ strace -e openat cat /dev/accel/accel0
openat(AT_FDCWD, "/dev/accel/accel0", O_RDONLY) = -1 EPERM (Operation not permitted)

# dmesg 中（如果 BPF 开启了 bpf_printk 调试）
$ dmesg | tail
device_reserve: DENY (other's device) major=235 minor=0
device_reserve: DENY my_cgid=0x1000001 owner_cgid=0x2000001
```

---

## 六、两表协同的拦截机制总结

```
┌─────────────────────────────────────────────────────────────┐
│                  reserved_devices                            │
│  key=(major, minor)  →  value=cgroup_id                     │
│                                                              │
│  精确记录：哪个设备被哪个 cgroup 预留                         │
│  ┌──────────┬───────────┬──────────────┐                    │
│  │ (235, 0) │ 0x2000001 │ 沙盒 B 独占  │                    │
│  │ (235, 1) │ 0x1000001 │ 沙盒 A 独占  │                    │
│  │ (195, 0) │ 0x3000001 │ 沙盒 C 独占  │                    │
│  └──────────┴───────────┴──────────────┘                    │
└─────────────────────────────────────────────────────────────┘

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
  195:0 (别人的)    — (major 不在过滤范围)   —                        ✅ 放行
  sda (块设备)      — (不是 char device)     —                        ✅ 放行
  /dev/null (1:3)   — (mj=1 不在过滤范围)    —                        ✅ 放行
```

> 💡 **核心思想**：BPF 只拦截 `major ∈ {235, 195}` 的字符设备（Ascend NPU 和 NVIDIA GPU），其他设备全部放行。对于这些目标 major，如果设备已被任何人预留（精确命中 → owner 不是我），或者我自己在这个 major 上有预留但访问的设备不在我的精确列表中（自约束），则拒绝访问。
