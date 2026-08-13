// 设备独占控制 - eBPF CGROUP_DEVICE (libbpf)
// 支持 major:minor 精确匹配 和 major:* 通配
//
// 编译: clang -O2 -g -target bpf -c device_block.bpf.c -o device_block.o
//
// 可选 CO-RE: 先 bpftool btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h
//            然后取消下面两行的注释，并删除本文件中所有手动定义的类型

// #include "vmlinux.h"

// ── 基础类型 (bpf_helpers.h 依赖这些类型，必须在 include 之前定义) ──

#ifndef __LINUX_TYPES_DEFINED__
#define __LINUX_TYPES_DEFINED__
typedef unsigned char      __u8;
typedef unsigned short     __u16;
typedef unsigned int       __u32;
typedef unsigned long long __u64;
typedef signed char        __s8;
typedef signed short       __s16;
typedef signed int         __s32;
typedef signed long long   __s64;

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
#define BPF_DEVCG_DEV_CHAR  2
#endif

// ── 设备号 key ─────────────────────────────────────────────────────

struct dev_key {
    __u32 major;
    __u32 minor;
};

// 通配 minor 的哨兵值 (0xFFFFFFFF)
#define MINOR_WILDCARD  ((__u32)-1)

char LICENSE[] SEC("license") = "GPL";

// ── BPF maps ───────────────────────────────────────────────────────

// 设备预留表: key=(major,minor) → value=cgroup_id
// minor=MINOR_WILDCARD 表示该 major 下所有 minor 都被预留
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
    __u32 __pad;  // 显式 padding，确保 16 字节对齐，避免 map key 哈希不一致
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key, struct cg_major_key);
    __type(value, __u8);
} reserved_majors SEC(".maps");

// ── BPF 程序入口 ───────────────────────────────────────────────────

SEC("cgroup/dev")
int device_reserve(struct bpf_cgroup_dev_ctx *ctx) {
    __u16 dev_type = ctx->access_type & 0xFFFF;

    // 只拦截字符设备，块设备直接放行
    if (dev_type != BPF_DEVCG_DEV_CHAR)
        return 1;  // 1 = 允许

    // bpf_printk("device_reserve: char dev major=%u minor=%u", ctx->major, ctx->minor);

    // 过滤掉一些特殊设备，避免误伤
    if ((ctx->major == 195 && ctx->minor == 255) || (ctx->major != 235 && ctx->major != 195)) {
        // bpf_printk("device_reserve: SKIP (special/other major) major=%u minor=%u", ctx->major, ctx->minor);
        return 1;
    }

    __u64 my_cgid = bpf_get_current_cgroup_id();
    // Docker/systemd 在沙盒下创建嵌套 cgroup（docker-xxx.scope），
    // 容器进程的实际 cgroup ID 与沙盒不同。也检查父 cgroup ID。
    // __u64 parent_cgid = bpf_get_current_ancestor_cgroup_id(1);

    // 1) 精确匹配 major:minor
    struct dev_key exact_key = { .major = ctx->major, .minor = ctx->minor };
    __u64 *owner = bpf_map_lookup_elem(&reserved_devices, &exact_key);

    if (owner) {
        // if (*owner == my_cgid || *owner == parent_cgid) {
        if (*owner == my_cgid){
            return 1;   // 我或父 cgroup 预留的 → 放行
        } else {
            return 0;   // 别人预留的 → 拒绝
        }
    }

    // 2) major 通配检查
    struct cg_major_key mk = { .cgid = my_cgid, .major = ctx->major, .__pad = 0 };
    // struct cg_major_key mk_parent = { .cgid = parent_cgid, .major = ctx->major, .__pad = 0 };
    __u8 *has_major = bpf_map_lookup_elem(&reserved_majors, &mk);
    // __u8 *has_major_parent = bpf_map_lookup_elem(&reserved_majors, &mk_parent);

    // if ((has_major && *has_major) || (has_major_parent && *has_major_parent)) {
    if (has_major && *has_major) {
        return 0;   // 该 major 被预留了，但不是我或父预留的 → 拒绝
    }

    // bpf_printk("device_reserve: ALLOW (unreserved) major=%u minor=%u cgid=%llu",ctx->major, ctx->minor, my_cgid);
    return 1;  // 未预留的 major，所有其他设备，一定要放行！！
}
