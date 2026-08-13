#!/bin/bash
# cgroup v1 沙盒 — 设备独占（白名单）+ CPU 限制 + 内存限制
#
# ===== 用法 =====
#
#   sudo ./sandbox.sh create  <name> <cpu> <mem> [major:minor ...]
#   sudo ./sandbox.sh join    <name> <PID>
#   sudo ./sandbox.sh status  <name>
#   sudo ./sandbox.sh destroy <name>
#   sudo ./sandbox.sh list
#   sudo ./sandbox.sh cleanup [--all]
#
#   cpu:    CPU 核数, 0 = 不限
#   mem:    内存, 如 512M / 2G / 0 (0 = 不限)
#   device: 沙盒独占的设备号，沙盒内仅可见这些设备；
#           沙盒外进程被移入 sandbox_default cgroup 后不可见这些设备
#
# ===== 示例 =====
#
#   # 2 核 CPU, 4G 内存, 独占所有 NPU
#   sudo ./sandbox.sh create test1 2 4G 235:* 236:*
#
#   # 1 核 CPU, 1G 内存, 不独占设备（全放行）
#   sudo ./sandbox.sh create test2 1 1G
#
#   # 不限 CPU/内存, 独占 GPU
#   sudo ./sandbox.sh create test3 0 0 195:* 226:*
#
# ===== 设备独占原理 =====
#
#   沙盒 cgroup:   先 allow 指定设备，再 deny 全部 → 仅可见指定设备
#   默认 cgroup:   deny 所有已被独占的设备 → 非沙盒进程不可见
#
#   非沙盒进程需移入 sandbox_default（devices 控制器）：
#     sudo ./sandbox.sh move-default <PID>
#
# ===== 常见设备号 =====
#   195:*  NVIDIA GPU      226:*  DRM GPU
#   235:*  Ascend NPU      236:*  Ascend Manager

set -e

# cgroup v1 各控制器挂载点
CGROUP_BASE="/sys/fs/cgroup"
CGROUP_CPU="${CGROUP_BASE}/cpu"
CGROUP_MEM="${CGROUP_BASE}/memory"
CGROUP_DEV="${CGROUP_BASE}/devices"

PREFIX="sandbox_"
DEFAULT_NAME="${PREFIX}default"

# 标记文件存放目录（cgroup 文件系统不允许创建自定义文件，故放 /tmp）
MARKER_DIR="/tmp/neu_box_sandbox_v1"
mkdir -p "$MARKER_DIR"

# 给定沙盒名 → 标记文件路径
marker_file() { echo "${MARKER_DIR}/${1}"; }

die()  { echo "错误: $*" >&2; exit 1; }
info() { echo "  $*"; }

# ── 内存解析: 512M / 2G → 字节 ──────────────────────────────────

parse_mem() {
    local raw="$1"
    [ "$raw" = "0" ] && { echo "0"; return; }
    [ -z "$raw" ] && { echo "0"; return; }

    local num="${raw//[^0-9]/}"
    local unit="${raw//[0-9]/}"
    case "$unit" in
        K|k) echo $(( num * 1024 )) ;;
        M|m) echo $(( num * 1024 * 1024 )) ;;
        G|g) echo $(( num * 1024 * 1024 * 1024 )) ;;
        *)   die "无法识别的内存单位: $raw (支持 K/M/G)" ;;
    esac
}

# ── 默认 cgroup 管理（用于隔离非沙盒进程的设备访问）─────────────

# 收集所有沙盒的独占设备（去重，用于重建默认 cgroup 的 deny 列表）
collect_all_exclusive_devices() {
    for marker in "${MARKER_DIR}"/*; do
        [ -f "$marker" ] || continue
        cat "$marker"
    done | sort -u
}

# 重建 sandbox_default cgroup 的设备拒绝列表
# 由于 cgroup v1 devices 的 deny/allow 是只写且不可删除单条，
# 重建时先移出进程 → 删除目录 → 重建目录 → 重新写入 deny → 移回进程
rebuild_default_deny() {
    local devpath="${CGROUP_DEV}/${DEFAULT_NAME}"
    local all_devices
    all_devices=$(collect_all_exclusive_devices)

    # 保存当前在默认 cgroup 中的进程
    local saved_procs=""
    if [ -d "$devpath" ]; then
        saved_procs=$(cat "${devpath}/cgroup.procs" 2>/dev/null || true)
        for p in $saved_procs; do
            echo "$p" > "${CGROUP_DEV}/cgroup.procs" 2>/dev/null || true
        done
        rmdir "$devpath" 2>/dev/null || true
    fi

    if [ -z "$all_devices" ]; then
        # 无独占设备 → 恢复进程到 root 即可，不需要默认 cgroup
        return
    fi

    # 重建并写入 deny 列表
    mkdir -p "$devpath"
    while IFS= read -r dev; do
        [ -n "$dev" ] || continue
        echo "c ${dev} rwm" > "${devpath}/devices.deny"
    done <<< "$all_devices"

    # 恢复进程
    for p in $saved_procs; do
        [ -d "/proc/$p" ] && echo "$p" > "${devpath}/cgroup.procs" 2>/dev/null || true
    done
}

# 确保默认 cgroup 存在
ensure_default_cgroup() {
    local devpath="${CGROUP_DEV}/${DEFAULT_NAME}"
    [ -d "$devpath" ] || mkdir -p "$devpath"
}

# 写入沙盒内必需放行的基础设备（在 deny all 之前调用）
# 否则 /dev/null /dev/pts 等都被禁，shell 基本操作都会失败
allow_baseline_devices() {
    local devpath="$1"
    # /dev/null, /dev/zero, /dev/full, /dev/random, /dev/urandom
    for dev in 1:3 1:5 1:7 1:8 1:9; do
        echo "c ${dev} rwm" > "${devpath}/devices.allow"
    done
    # /dev/tty, /dev/console, /dev/ptmx
    for dev in 5:0 5:1 5:2; do
        echo "c ${dev} rwm" > "${devpath}/devices.allow"
    done
    # /dev/pts/*
    echo "c 136:* rwm" > "${devpath}/devices.allow"
    # /dev/ttyN (virtual terminals)
    echo "c 4:* rwm" > "${devpath}/devices.allow"
}

# ── create ──────────────────────────────────────────────────────

cmd_create() {
    local name="$1";  [ -n "$name" ] || die "用法: $0 create <name> <cpu> <mem> [major:minor ...]"
    local cpu="$2";   [ -n "$cpu"  ] || die "用法: $0 create <name> <cpu> <mem> [major:minor ...]"
    local mem="$3";   [ -n "$mem"  ] || die "用法: $0 create <name> <cpu> <mem> [major:minor ...]"
    shift 3  # 剩余参数都是设备号

    local mem_bytes
    mem_bytes=$(parse_mem "$mem")

    # --- 创建 cgroup 目录 ---
    local cpupath="${CGROUP_CPU}/${PREFIX}${name}"
    local mempath="${CGROUP_MEM}/${PREFIX}${name}"
    local devpath="${CGROUP_DEV}/${PREFIX}${name}"

    [ ! -d "$cpupath" ] || die "沙盒 '${name}' 已存在"
    mkdir -p "$cpupath" "$mempath" "$devpath"

    # --- CPU 限制 ---
    if [ "$cpu" != "0" ] && [ -n "$cpu" ]; then
        echo 100000 > "${cpupath}/cpu.cfs_period_us"
        echo $(( cpu * 100000 )) > "${cpupath}/cpu.cfs_quota_us"
        info "CPU: ${cpu} 核 (quota=$(( cpu * 100000 ))/period=100000)"
    else
        info "CPU: 不限"
    fi

    # --- 内存限制 ---
    if [ "$mem_bytes" != "0" ] && [ -n "$mem_bytes" ]; then
        echo "$mem_bytes" > "${mempath}/memory.limit_in_bytes"
        echo 0 > "${mempath}/memory.swappiness" 2>/dev/null || true
        info "内存: ${mem} (${mem_bytes} bytes)"
    else
        info "内存: 不限"
    fi

    # --- 设备独占（白名单模式）---
    if [ $# -gt 0 ]; then
        # 关键顺序：先 allow 基础设备+独占设备，再 deny 全部
        # cgroup v1 devices 按写入顺序逐条匹配，首条命中即生效
        # 先把这个 cgroup 设为默认拒绝所有设备
        echo "a *:* rwm" > "${devpath}/devices.deny"

        # 再调用放行基础设备
        allow_baseline_devices "$devpath"

        # 最后放行你指定的独占设备
        for dev in "$@"; do
            echo "c ${dev} rwm" > "${devpath}/devices.allow"
        done

        # 保存独占设备列表（用于后续重建默认 cgroup deny 列表）
        printf '%s\n' "$@" > "$(marker_file "$name")"

        # 将独占设备加入默认 cgroup 的 deny 列表
        ensure_default_cgroup
        rebuild_default_deny

        info "独占设备: $*"
        info "  (沙盒内: 除基础设备外仅可见上述独占设备; 沙盒外: 上述独占设备不可见)"
    else
        info "设备: 全放行（无独占设备）"
        rm -f "$(marker_file "$name")"
    fi

    echo "✓ 沙盒已创建: ${CGROUP_BASE}/*/${PREFIX}${name}"
    echo "  加入进程: sudo $0 join $name <PID>"
}

# ── join ────────────────────────────────────────────────────────

cmd_join() {
    local name="$1" pid="$2"
    [ -n "$name" ] || die "用法: $0 join <name> <PID>"
    [ -n "$pid"  ] || die "用法: $0 join <name> <PID>"
    [ -d "/proc/$pid" ] || die "PID $pid 不存在"

    local cpupath="${CGROUP_CPU}/${PREFIX}${name}"
    local mempath="${CGROUP_MEM}/${PREFIX}${name}"
    local devpath="${CGROUP_DEV}/${PREFIX}${name}"

    [ -d "$cpupath" ] || die "沙盒 '${name}' 不存在"

    echo "$pid" > "${cpupath}/cgroup.procs"
    echo "$pid" > "${mempath}/cgroup.procs"
    echo "$pid" > "${devpath}/cgroup.procs"
    echo "✓ PID $pid 已加入沙盒 '${name}' (cpu + memory + devices)"
}

# ── move-default ────────────────────────────────────────────────
# 将进程从沙盒（或 root）移到默认设备 cgroup（非沙盒环境）

cmd_move_default() {
    local pid="$1"
    [ -n "$pid" ] || die "用法: $0 move-default <PID>"
    [ -d "/proc/$pid" ] || die "PID $pid 不存在"

    local devpath="${CGROUP_DEV}/${DEFAULT_NAME}"
    if [ ! -d "$devpath" ]; then
        info "默认设备 cgroup 不存在，正在创建..."
        ensure_default_cgroup
        rebuild_default_deny
    fi

    echo "$pid" > "${devpath}/cgroup.procs"
    echo "✓ PID $pid 已移入默认设备 cgroup（不可见已被沙盒独占的设备）"
}

# ── status ──────────────────────────────────────────────────────

cmd_status() {
    local name="$1"; [ -n "$name" ] || die "用法: $0 status <name>"
    local cpupath="${CGROUP_CPU}/${PREFIX}${name}"
    [ -d "$cpupath" ] || die "沙盒 '${name}' 不存在"

    echo "=== 沙盒: $name ==="
    echo ""
    echo "--- CPU ---"
    echo "  quota:  $(cat ${cpupath}/cpu.cfs_quota_us 2>/dev/null || echo '-')"
    echo "  period: $(cat ${cpupath}/cpu.cfs_period_us 2>/dev/null || echo '-')"
    echo ""
    echo "--- 内存 ---"
    local mempath="${CGROUP_MEM}/${PREFIX}${name}"
    local limit
    limit=$(cat "${mempath}/memory.limit_in_bytes" 2>/dev/null || echo '-')
    if [ "$limit" = "9223372036854771712" ] || [ "$limit" = "-1" ]; then
        echo "  limit:  不限"
    else
        echo "  limit:  $limit bytes ($(( limit / 1024 / 1024 ))M)"
    fi
    local usage
    usage=$(cat "${mempath}/memory.usage_in_bytes" 2>/dev/null || echo '-')
    echo "  usage:  ${usage} bytes"
    echo ""
    echo "--- 设备 ---"
    local marker; marker=$(marker_file "$name")
    if [ -f "$marker" ] && [ -s "$marker" ]; then
        echo "  独占设备: $(tr '\n' ' ' < "$marker")"
        echo "  模式: 白名单（仅可见上述设备）"
    else
        echo "  模式: 全放行（无设备限制）"
    fi
    echo ""
    echo "--- 进程列表 ---"
    local procs
    procs=$(cat "${cpupath}/cgroup.procs" 2>/dev/null)
    if [ -z "$procs" ]; then
        echo "  (空)"
    else
        for p in $procs; do
            if [ -f "/proc/$p/cmdline" ]; then
                printf "  PID %-8s %s\n" "$p" "$(tr '\0' ' ' < "/proc/$p/cmdline")"
            else
                echo "  PID $p  (已退出)"
            fi
        done
    fi
}

# ── destroy ─────────────────────────────────────────────────────

cmd_destroy() {
    local name="$1"; [ -n "$name" ] || die "用法: $0 destroy <name>"
    local cpupath="${CGROUP_CPU}/${PREFIX}${name}"
    [ -d "$cpupath" ] || die "沙盒 '${name}' 不存在"

    local devpath="${CGROUP_DEV}/${PREFIX}${name}"
    local had_exclusive=false
    [ -f "$(marker_file "$name")" ] && had_exclusive=true

    # 踢出所有进程（移回父 cgroup）
    for cg in "$cpupath" "${CGROUP_MEM}/${PREFIX}${name}" "${CGROUP_DEV}/${PREFIX}${name}"; do
        local procs
        procs=$(cat "${cg}/cgroup.procs" 2>/dev/null || true)
        for p in $procs; do
            echo "$p" > "$(dirname "$cg")/cgroup.procs" 2>/dev/null || true
        done
        rmdir "$cg" 2>/dev/null || true
    done

    # 删除标记文件
    rm -f "$(marker_file "$name")"

    # 更新默认 cgroup：移除该沙盒独占设备对应的 deny 条目
    if $had_exclusive; then
        rebuild_default_deny
    fi

    echo "✓ 沙盒 '${name}' 已销毁"
}

# ── list ────────────────────────────────────────────────────────

cmd_list() {
    echo "=== 沙盒列表 ==="
    local found=false

    for sandbox_dir in "${CGROUP_CPU}/${PREFIX}"*; do
        [ -d "$sandbox_dir" ] || continue
        found=true
        local sname="${sandbox_dir##*/${PREFIX}}"

        # CPU 信息
        local quota period cpu_info
        quota=$(cat "${sandbox_dir}/cpu.cfs_quota_us" 2>/dev/null || echo '-')
        period=$(cat "${sandbox_dir}/cpu.cfs_period_us" 2>/dev/null || echo '-')
        if [ "$quota" = "-1" ] || [ "$quota" = "-" ]; then
            cpu_info="不限"
        else
            cpu_info="$(( quota / 100000 )) 核"
        fi

        # 内存信息
        local mempath="${CGROUP_MEM}/${PREFIX}${sname}"
        local limit mem_info usage
        limit=$(cat "${mempath}/memory.limit_in_bytes" 2>/dev/null || echo '-')
        usage=$(cat "${mempath}/memory.usage_in_bytes" 2>/dev/null || echo '0')
        if [ "$limit" = "9223372036854771712" ] || [ "$limit" = "-1" ] || [ "$limit" = "-" ]; then
            mem_info="不限"
        else
            mem_info="$(( limit / 1024 / 1024 ))M"
        fi
        local usage_mb=$(( usage / 1024 / 1024 ))

        # 独占设备
        local marker; marker=$(marker_file "$sname")
        local dev_info
        if [ -f "$marker" ] && [ -s "$marker" ]; then
            dev_info=$(tr '\n' ' ' < "$marker")
        else
            dev_info="(无)"
        fi

        # 进程
        local procs procs_count
        procs=$(cat "${sandbox_dir}/cgroup.procs" 2>/dev/null || true)
        if [ -z "$procs" ]; then
            procs_count=0
        else
            procs_count=$(echo "$procs" | wc -l)
        fi

        echo ""
        echo "  [$sname]"
        echo "    CPU:      $cpu_info"
        echo "    内存:     $mem_info  (已用 ${usage_mb}M)"
        echo "    独占设备: $dev_info"
        echo "    进程数:   $procs_count"

        if [ "$procs_count" -gt 0 ]; then
            for p in $procs; do
                if [ -f "/proc/$p/cmdline" ]; then
                    printf "      PID %-8s %s\n" "$p" "$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)"
                fi
            done
        fi
    done

    if ! $found; then
        echo "  (无沙盒)"
    fi

    # 设备独占总览
    echo ""
    echo "--- 设备独占总览 ---"
    local all_devices
    all_devices=$(collect_all_exclusive_devices)
    if [ -n "$all_devices" ]; then
        echo "  已被独占的设备: $(echo "$all_devices" | tr '\n' ' ')"
        local devpath="${CGROUP_DEV}/${DEFAULT_NAME}"
        if [ -d "$devpath" ]; then
            local def_procs
            def_procs=$(cat "${devpath}/cgroup.procs" 2>/dev/null || true)
            if [ -z "$def_procs" ]; then
                echo "  sandbox_default 中无进程（非沙盒进程可能仍可见独占设备！）"
                echo "  将进程移入: sudo $0 move-default <PID>"
            else
                local def_count
                def_count=$(echo "$def_procs" | wc -l)
                echo "  sandbox_default 中有 ${def_count} 个进程（不可见上述设备）"
            fi
        else
            echo "  sandbox_default 不存在"
        fi
    else
        echo "  无设备被独占"
    fi
}

# ── cleanup ─────────────────────────────────────────────────────

cmd_cleanup() {
    local mode="${1:-}"

    if [ "$mode" = "--all" ]; then
        echo "=== 清理所有沙盒 ==="
        local cleaned=0
        for sandbox_dir in "${CGROUP_CPU}/${PREFIX}"*; do
            [ -d "$sandbox_dir" ] || continue
            local sname="${sandbox_dir##*/${PREFIX}}"
            echo "  销毁: $sname"
            _destroy_internal "$sname"
            cleaned=$((cleaned + 1))
        done
        if [ "$cleaned" -eq 0 ]; then
            echo "  (无沙盒)"
        else
            echo "✓ 已清理 $cleaned 个沙盒"
        fi
    else
        echo "=== 清理空沙盒 ==="
        local cleaned=0
        for sandbox_dir in "${CGROUP_CPU}/${PREFIX}"*; do
            [ -d "$sandbox_dir" ] || continue
            local sname="${sandbox_dir##*/${PREFIX}}"

            local procs
            procs=$(cat "${sandbox_dir}/cgroup.procs" 2>/dev/null || true)
            if [ -z "$procs" ]; then
                echo "  销毁空沙盒: $sname"
                _destroy_internal "$sname"
                cleaned=$((cleaned + 1))
            fi
        done
        if [ "$cleaned" -eq 0 ]; then
            echo "  (无空沙盒)"
        else
            echo "✓ 已清理 $cleaned 个空沙盒"
        fi
    fi
}

# 内部销毁函数（供 cleanup 循环使用，逐沙盒清理后不重复重建 default）
_destroy_internal() {
    local name="$1"
    local cpupath="${CGROUP_CPU}/${PREFIX}${name}"
    [ -d "$cpupath" ] || return

    local had_exclusive=false
    [ -f "$(marker_file "$name")" ] && had_exclusive=true

    for cg in "$cpupath" "${CGROUP_MEM}/${PREFIX}${name}" "${CGROUP_DEV}/${PREFIX}${name}"; do
        local procs
        procs=$(cat "${cg}/cgroup.procs" 2>/dev/null || true)
        for p in $procs; do
            echo "$p" > "$(dirname "$cg")/cgroup.procs" 2>/dev/null || true
        done
        rmdir "$cg" 2>/dev/null || true
    done

    rm -f "$(marker_file "$name")"

    if $had_exclusive; then
        rebuild_default_deny
    fi
}

# ── main ───────────────────────────────────────────────────────

case "${1:-}" in
    create)
        shift
        cmd_create "$@"
        ;;
    join)
        cmd_join "${2:-}" "${3:-}"
        ;;
    move-default)
        cmd_move_default "${2:-}"
        ;;
    status)
        cmd_status "${2:-}"
        ;;
    destroy)
        cmd_destroy "${2:-}"
        ;;
    list)
        cmd_list
        ;;
    cleanup)
        shift
        cmd_cleanup "$@"
        ;;
    *)
        echo "用法: $0 {create|join|move-default|status|destroy|list|cleanup} [args...]"
        echo ""
        echo "  create  <name> <cpu> <mem> [major:minor ...]"
        echo "          cpu    = CPU 核数 (0=不限)"
        echo "          mem    = 内存大小 (如 512M, 2G, 0=不限)"
        echo "          device = 独占设备号 (如 235:*)"
        echo "  join    <name> <PID>        — 将进程加入沙盒"
        echo "  move-default <PID>          — 将进程移入默认设备 cgroup"
        echo "  status  <name>              — 查看沙盒状态"
        echo "  destroy <name>              — 销毁沙盒"
        echo "  list                        — 列出所有沙盒"
        echo "  cleanup                     — 清理空沙盒"
        echo "  cleanup --all               — 清理所有沙盒"
        echo ""
        echo "  示例:"
        echo "    $0 create gpu_box  2 4G 195:* 226:*"
        echo "    $0 create npu_box  1 1G 235:* 236:*"
        echo "    $0 create basic    1 512M"
        echo "    $0 create unlimited 0 0 235:*"
        ;;
esac
