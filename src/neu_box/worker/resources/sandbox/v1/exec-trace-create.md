# `bash sandbox.sh create test1 0 0 235:1` 执行命令追踪

## 脚本调用参数

```
bash sandbox.sh create test1 0 0 235:1
```

进入 main 后 `shift`，`cmd_create test1 0 0 235:1` 被调用。

参数解析：
- `name` = `test1`
- `cpu`  = `0`
- `mem`  = `0`
- 独占设备 = `235:1`（`$@` 剩余参数）

---

## 实际顺序执行的命令

> 以下按脚本执行顺序逐条列出，包括条件分支内实际执行的部分。假定沙盒 `test1` 尚未存在，且系统上此前无其他沙盒独占设备。

### 1. 脚本顶层初始化（任何子命令都会执行）

| 序号 | 命令 | 说明 |
|------|------|------|
| 1 | `mkdir -p /tmp/neu_box_sandbox_v1` | 创建标记文件存放目录（第 54 行，脚本顶层直接执行） |

### 2. `cmd_create` 函数体

| 序号 | 命令 | 说明 |
|------|------|------|
| 2 | `parse_mem "0"` → 返回 `"0"` | 内存解析：`0` 直接返回 `0`（第 66 行） |
| 3 | `mkdir -p /sys/fs/cgroup/cpu/sandbox_test1 /sys/fs/cgroup/memory/sandbox_test1 /sys/fs/cgroup/devices/sandbox_test1` | 创建三个 cgroup 控制器目录（第 166 行） |

### 3. CPU 限制分支

> `cpu = 0` → 走 else 分支，不设置 cgroup 参数，仅输出 info。

| 序号 | 命令 | 说明 |
|------|------|------|
| 4 | （无 cgroup 写入）`info "CPU: 不限"` | 仅打印日志（第 174 行） |

### 4. 内存限制分支

> `mem_bytes = 0` → 走 else 分支，不设置 cgroup 参数，仅输出 info。

| 序号 | 命令 | 说明 |
|------|------|------|
| 5 | （无 cgroup 写入）`info "内存: 不限"` | 仅打印日志（第 183 行） |

### 5. 设备独占分支（白名单模式）

> `$# = 1`（有 `235:1`），进入设备独占逻辑。

**5a. 先 deny 全部设备**

| 序号 | 命令 | 说明 |
|------|------|------|
| 6 | `echo "a *:* rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.deny` | 默认拒绝所有设备访问（第 191 行） |

**5b. 放行基础设备**（调用 `allow_baseline_devices`，第 134-147 行）

| 序号 | 命令 | 说明 |
|------|------|------|
| 7 | `echo "c 1:3 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/null |
| 8 | `echo "c 1:5 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/zero |
| 9 | `echo "c 1:7 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/full |
| 10 | `echo "c 1:8 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/random |
| 11 | `echo "c 1:9 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/urandom |
| 12 | `echo "c 5:0 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/tty |
| 13 | `echo "c 5:1 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/console |
| 14 | `echo "c 5:2 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/ptmx |
| 15 | `echo "c 136:* rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/pts/* |
| 16 | `echo "c 4:* rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | /dev/ttyN (virtual terminals) |

**5c. 放行独占设备**

| 序号 | 命令 | 说明 |
|------|------|------|
| 17 | `echo "c 235:1 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow` | 放行用户指定的独占设备 235:1（第 198 行） |

**5d. 保存独占设备标记**

| 序号 | 命令 | 说明 |
|------|------|------|
| 18 | `printf '%s\n' "235:1" > /tmp/neu_box_sandbox_v1/test1` | 将独占设备号写入标记文件（第 202 行） |

**5e. 确保默认设备 cgroup 存在**（调用 `ensure_default_cgroup`，第 126-129 行）

| 序号 | 命令 | 说明 |
|------|------|------|
| 19 | `mkdir -p /sys/fs/cgroup/devices/sandbox_default` | 确保默认 cgroup 目录存在（第 128 行） |

**5f. 重建默认 cgroup 的 deny 列表**（调用 `rebuild_default_deny`，第 92-123 行）

| 序号 | 命令 | 说明 |
|------|------|------|
| 20 | `cat /tmp/neu_box_sandbox_v1/test1` → `235:1` | `collect_all_exclusive_devices` 收集所有沙盒的独占设备，此处仅 test1（第 83-87 行） |
| 21 | `cat /sys/fs/cgroup/devices/sandbox_default/cgroup.procs` → 空 | 保存默认 cgroup 中的进程列表（第 100 行，刚创建的空目录） |
| 22 | `rmdir /sys/fs/cgroup/devices/sandbox_default` | 删除默认 cgroup 目录以重建（第 104 行） |
| 23 | `mkdir -p /sys/fs/cgroup/devices/sandbox_default` | 重建默认 cgroup 目录（第 113 行） |
| 24 | `echo "c 235:1 rwm" > /sys/fs/cgroup/devices/sandbox_default/devices.deny` | 在默认 cgroup 中 deny 独占用设备（第 116 行，循环仅一个设备） |

### 6. 打印完成信息

| 序号 | 命令 | 说明 |
|------|------|------|
| 25 | `info "独占设备: 235:1"` | 打印独占设备信息（第 208 行） |
| 26 | `echo "✓ 沙盒已创建: /sys/fs/cgroup/*/sandbox_test1"` | 打印创建成功及加入进程提示（第 215-216 行） |

---

## 汇总：实际写入 cgroup 文件系统的操作

### sandbox_test1（沙盒 cgroup）

```bash
# 1. 创建目录
mkdir -p /sys/fs/cgroup/cpu/sandbox_test1 \
         /sys/fs/cgroup/memory/sandbox_test1 \
         /sys/fs/cgroup/devices/sandbox_test1

# 2. 设备控制 — 先 deny 全部，再 allow 白名单
echo "a *:* rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.deny

# 3. 放行基础设备（共 10 条）
echo "c 1:3 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/null
echo "c 1:5 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/zero
echo "c 1:7 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/full
echo "c 1:8 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/random
echo "c 1:9 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/urandom
echo "c 5:0 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/tty
echo "c 5:1 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/console
echo "c 5:2 rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/ptmx
echo "c 136:* rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/pts/*
echo "c 4:* rwm"   > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # /dev/ttyN

# 4. 放行独占设备
echo "c 235:1 rwm" > /sys/fs/cgroup/devices/sandbox_test1/devices.allow   # 用户指定

# 5. 保存标记
printf '%s\n' "235:1" > /tmp/neu_box_sandbox_v1/test1
```

### sandbox_default（默认 cgroup，隔离非沙盒进程）

```bash
# 6. 先建、再删、再建（为了清理旧的 deny 规则）
mkdir -p /sys/fs/cgroup/devices/sandbox_default
rmdir /sys/fs/cgroup/devices/sandbox_default
mkdir -p /sys/fs/cgroup/devices/sandbox_default

# 7. 拒绝已被沙盒独占的设备
echo "c 235:1 rwm" > /sys/fs/cgroup/devices/sandbox_default/devices.deny
```

---

## 关键设计说明

1. **cgroup v1 devices 白名单顺序**：必须先写 `devices.deny` 再写 `devices.allow`，因为 cgroup v1 的 devices 子系统按写入顺序逐条匹配，首条命中即生效。因此先 `deny a *:*` 全部拒绝，再逐条 `allow` 白名单设备。

2. **基础设备的必要性**：如果不在白名单中放行 `/dev/null`、`/dev/pts` 等基础设备，沙盒内的 shell 连基本 I/O 操作都会失败。

3. **sandbox_default 的作用**：当沙盒独占某设备后，非沙盒进程需要被移入 `sandbox_default` cgroup，该 cgroup 的 `devices.deny` 会阻止这些进程访问已被独占的设备，实现设备隔离。

4. **CPU/内存不限**：`cpu=0` 和 `mem=0` 时跳过 cgroup 参数设置，沙盒内进程不受 CPU 和内存限制。
