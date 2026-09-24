// 设备独占控制 - eBPF CGROUP_DEVICE (libbpf)
//
// 授权模型
//   宿主机进程: 授权挂在沙盒 cgroup 上，按 reserved_devices / reserved_majors
//               逐卡判定（与历史行为一致）。
//   容器进程  : 容器不持有授权，只持有"受托身份"。判定分两步 —— 先用
//               mount namespace 认出"我是容器"，再用 container_owner 查出
//               委托方沙盒，最后确认该沙盒持有这张卡。
//
// 为什么容器不能沿用宿主机规则: 宿主机规则对"无人预留的设备"放行，而容器
// 必须一律拒绝（RFC 要求未经 neu-box 的容器不得访问任何 Ascend 设备）。
// 这条规则表达不出"这里本来就不该有容器"，因此容器需要正着判出来。
//
// 编译: clang -O2 -g -target bpf -c device_block.bpf.c -o device_block.o
//       （-g 必须保留: 读取 mnt ns 需要 CO-RE，见下）

// #include "vmlinux.h"   // 本文件用手写类型，不引入 vmlinux.h

// ── 基础类型 (bpf_helpers.h 依赖这些类型，必须在 include 之前定义) ──

#ifndef __LINUX_TYPES_DEFINED__
#define __LINUX_TYPES_DEFINED__
typedef unsigned char __u8;
typedef unsigned short __u16;
typedef unsigned int __u32;
typedef unsigned long long __u64;
typedef signed char __s8;
typedef signed short __s16;
typedef signed int __s32;
typedef signed long long __s64;

// 网络字节序类型 (内核中用 __bitwise 标记，BPF 编译时等价于基础类型)
typedef __u16 __be16;
typedef __u32 __be32;
typedef __u32 __wsum;
#endif

// BPF map 类型枚举 (来自 include/uapi/linux/bpf.h，bpf_helpers.h 不提供)
#ifndef BPF_MAP_TYPE_HASH
#define BPF_MAP_TYPE_HASH 1
#endif

#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>

// ── 内核类型定义 (使用 vmlinux.h 时不需要，可删除以下全部) ──────────

#ifndef __bpf_cgroup_dev_ctx_defined
#define __bpf_cgroup_dev_ctx_defined
// struct bpf_cgroup_dev_ctx (来自 include/uapi/linux/bpf.h)
struct bpf_cgroup_dev_ctx {
    __u32 access_type;
    __u32 major;
    __u32 minor;
};
#endif

#ifndef BPF_DEVCG_DEV_BLOCK
#define BPF_DEVCG_DEV_BLOCK 1
#define BPF_DEVCG_DEV_CHAR 2
#endif

// CO-RE 用的最小内核类型声明。这里只声明判定需要的字段路径，偏移由
// libbpf 在加载时按目标内核的 BTF 重定位，因此不需要 vmlinux.h。
// 字段名必须与内核 BTF 一致: task_struct.nsproxy → nsproxy.mnt_ns →
// mnt_namespace.ns → ns_common.inum。
struct ns_common {
    unsigned int inum;
};

struct mnt_namespace {
    struct ns_common ns;
};

struct nsproxy {
    struct mnt_namespace* mnt_ns;
};

struct task_struct {
    struct nsproxy* nsproxy;
};

// ── 设备号 key ─────────────────────────────────────────────────────

struct dev_key {
    __u32 major;
    __u32 minor;
};

char LICENSE[] SEC("license") = "GPL";

// ── 运行期配置 (const volatile，加载前由 native CLI 写入 .rodata) ────
//
// 顺序和 padding 与 native/sandbox/src/sandbox.hpp 的 BpfConfig 一一对应；
// 两侧由 static_assert + 加载时的 value_size 检查保证一致。

struct bpf_config {
    __u32 devdrv_major; // 受管设备 major，0 表示没有受管设备
    __u32 __pad;        // 显式占位，让 host_mnt_ns 落在 8 字节边界
    __u64 host_mnt_ns;  // 宿主机 mount namespace 的 inum，0 = 尚未配置
};

const volatile struct bpf_config config = {};

// ── BPF maps ───────────────────────────────────────────────────────

// 设备预留表: key=(major,minor) → value=cgroup_id
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, struct dev_key);
    __type(value, __u64);
} reserved_devices SEC(".maps");

// 预留涉及的 major 集合: key=(cgroup_id, major) → value=1
// 限制粒度到 major 级别: 沙盒进程只在该 major 内被限制，其他 major 不受影响
struct cg_major_key {
    __u64 cgid;
    __u32 major;
    __u32 __pad; // 显式 padding，确保 16 字节对齐，避免 map key 哈希不一致
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, struct cg_major_key);
    __type(value, __u8);
} reserved_majors SEC(".maps");

// 容器归属表: key=容器 init 的 mount namespace inum → value=委托方沙盒 cgroup_id
// 由 native CLI 在容器放行之前写入（bind-container），容器退出时删除。
// 容器不持有授权，它只是受托方: 沙盒结束即失去这条记录。
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key, __u64);
    __type(value, __u64);
} container_owner SEC(".maps");

// ── 内部辅助 ───────────────────────────────────────────────────────

// 当前进程的 mount namespace inum。
// 用户态用 stat("/proc/<pid>/ns/mnt").st_ino 取到的是同一个数，无需换算。
static __always_inline __u64 current_mount_namespace(void) {
    struct task_struct* task = (struct task_struct*)bpf_get_current_task_btf();
    return BPF_CORE_READ(task, nsproxy, mnt_ns, ns.inum);
}

// ── BPF 程序入口 ───────────────────────────────────────────────────

SEC("cgroup/dev")
int device_reserve(struct bpf_cgroup_dev_ctx* ctx) {
    __u16 dev_type = ctx->access_type & 0xFFFF;

    // 只拦截字符设备，块设备直接放行
    if (dev_type != BPF_DEVCG_DEV_CHAR)
        return 1; // 1 = 允许

    // 保留原有范围：NVIDIA control device 共享；除 NVIDIA 和
    // devdrv-cdev 外的其他字符设备不参与隔离。
    if ((ctx->major == 195 && ctx->minor == 255) ||
        (ctx->major != 195 && (config.devdrv_major == 0 || ctx->major != config.devdrv_major))) {
        return 1;
    }

    struct dev_key exact_key = {.major = ctx->major, .minor = ctx->minor};
    __u64 my_mnt_ns = current_mount_namespace();

    // mount namespace 与宿主机不同 → 容器进程。
    // host_mnt_ns 为 0（未配置）时宿主机进程也会落到这一支，结果是全部拒绝；
    // 方向是 fail-closed，且加载期就会暴露，不会静默放行。
    if (my_mnt_ns != config.host_mnt_ns) {
        // 容器: 授权只来自委托方，不来自"设备空闲"，也不来自 cgroup 位置。
        __u64* sandbox = bpf_map_lookup_elem(&container_owner, &my_mnt_ns);
        if (!sandbox)
            return 0; // 没有归属登记 → 一律拒绝

        __u64* container_owner_cgid = bpf_map_lookup_elem(&reserved_devices, &exact_key);
        if (container_owner_cgid && *container_owner_cgid == *sandbox)
            return 1; // 委托方持有这张卡 → 放行

        return 0; // 空闲 / 别人持有 → 拒绝
    }

    // ── 宿主机进程: 与历史行为逐字一致 ──
    // 沙盒 shell 是被 join 直接写进沙盒 cgroup 的（写 cgroup.procs），
    // 不存在嵌套层级，所以只做精确匹配、不向上找祖先。
    __u64 my_cgid = bpf_get_current_cgroup_id();

    // 1) 精确匹配 major:minor
    __u64* owner = bpf_map_lookup_elem(&reserved_devices, &exact_key);
    if (owner) {
        if (*owner == my_cgid)
            return 1; // 自己预留的 → 放行
        return 0;     // 别人预留的 → 拒绝
    }

    // 2) 该 major 上有预留、但这张卡没拿到 → 拒绝
    struct cg_major_key mk = {.cgid = my_cgid, .major = ctx->major, .__pad = 0};
    __u8* has_major = bpf_map_lookup_elem(&reserved_majors, &mk);
    if (has_major && *has_major)
        return 0;

    return 1; // 未预留的 major，所有其他设备，一定要放行！！
}
