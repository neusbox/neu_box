#!/bin/bash
# eBPF CGROUP_DEVICE 沙盒 — 设备独占 + CPU 限制 + 内存限制
# cgroup v2 only
#
# 运行依赖: bpftool。发布包包含构建阶段预编译的 device_block.o。
#
# ===== 用法 =====
#
#   sudo ./sandbox.sh create <name> <cpu> <mem> [major:minor ...]
#   sudo ./sandbox.sh join   <name> <PID>
#   sudo ./sandbox.sh status <name>
#   sudo ./sandbox.sh destroy <name>
#   sudo ./sandbox.sh list
#   sudo ./sandbox.sh cleanup
#
#   cpu: 核数 (0=不限)    mem: 如 512M / 2G / 0
#   设备号: major:minor (如 235:0, 235:*, 235)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BPF_OBJ="${SCRIPT_DIR}/device_block.o"
BPF_PIN="/sys/fs/bpf/device_block"
MAP_DIR="/sys/fs/bpf/sandbox_maps"
MAP_RESERVED_PIN="${MAP_DIR}/reserved_devices"
MAP_MAJORS_PIN="${MAP_DIR}/reserved_majors"

CGROUP_ROOT="/sys/fs/cgroup"
PREFIX="sandbox_"

die() { echo "错误: $*" >&2; exit 1; }

# 检查是否具备 root 等效权限（能操作 cgroup）
_require_root() {
    if [ "$(id -u)" -ne 0 ] && [ ! -w "${CGROUP_ROOT}/cgroup.procs" ]; then
        die "需要 root 权限，请使用: sudo $0 $*"
    fi
}

# ==================================================================
# cgroup v2 路径函数
# ==================================================================
CGROUP_VER=2

_cg()        { echo "${CGROUP_ROOT}/${PREFIX}${1}"; }
_cg_cpu()    { echo "${CGROUP_ROOT}/${PREFIX}${1}/cpu.max"; }
_cg_mem()    { echo "${CGROUP_ROOT}/${PREFIX}${1}/memory.max"; }
_cg_memcur() { echo "${CGROUP_ROOT}/${PREFIX}${1}/memory.current"; }
_cg_swap()   { echo "${CGROUP_ROOT}/${PREFIX}${1}/memory.swap.max"; }
_cg_procs()  { echo "${CGROUP_ROOT}/${PREFIX}${1}/cgroup.procs"; }
_cg_dev()    { echo "${CGROUP_ROOT}/${PREFIX}${1}"; }
_cg_root_procs() { echo "${CGROUP_ROOT}/cgroup.procs"; }
_cg_enable() {
    echo "+cpu +memory" > "${CGROUP_ROOT}/cgroup.subtree_control" 2>/dev/null || true
}
_cg_mkdir() {
    local cg=$(_cg "$1"); mkdir -p "$cg"
}
_cg_rmdir() {
    local cg=$(_cg "$1")
    rmdir "$cg" 2>/dev/null && return 0
    # systemd slice 可能阻止 rmdir，先停 slice 再重试
    _unregister_systemd_slice "$1"
    sleep 0.1
    rmdir "$cg" 2>/dev/null || true
}

_register_systemd_slice() {
    local slice_name="sandbox_${1}"
    busctl call \
        org.freedesktop.systemd1 \
        /org/freedesktop/systemd1 \
        org.freedesktop.systemd1.Manager \
        StartTransientUnit \
        "ssa(sv)a(sa(sv))" \
        "$slice_name" "replace" \
        0 0 2>/dev/null || true
}

_unregister_systemd_slice() {
    local slice_name="sandbox_${1}"
    busctl call \
        org.freedesktop.systemd1 \
        /org/freedesktop/systemd1 \
        org.freedesktop.systemd1.Manager \
        StopUnit \
        "ss" "$slice_name" "replace" 2>/dev/null || true
}
_cg_join() {
    local name="$1" pid="$2"
    echo "$pid" > "$(_cg_procs "$name")"
}
_cg_kickall() {
    # 旧名保留，内部转发到 _cg_kill_all
    _cg_kill_all "$@"
}
_cg_all_procs() {
    # cgroup.procs 只列当前层级；递归读取，避免容器创建子 cgroup 后漏杀。
    local name="$1" cg procs_file
    cg=$(_cg "$name")
    [ -d "$cg" ] || return 0
    while IFS= read -r procs_file; do
        cat "$procs_file" 2>/dev/null || true
    done < <(find "$cg" -type f -name cgroup.procs 2>/dev/null)
}
_cg_is_populated() {
    local name="$1" cg value
    cg=$(_cg "$name")
    [ -d "$cg" ] || { echo 0; return; }
    value=$(awk '$1 == "populated" {print $2}' "$cg/cgroup.events" 2>/dev/null || true)
    if [ -n "$value" ]; then
        echo "$value"
    elif [ -n "$(_cg_all_procs "$name")" ]; then
        echo 1
    else
        echo 0
    fi
}
_cg_remove_children() {
    # 最深层优先删除空的子 cgroup，否则主目录会永久 rmdir 失败。
    local name="$1" cg child
    cg=$(_cg "$name")
    [ -d "$cg" ] || return 0
    while IFS= read -r child; do
        rmdir "$child" 2>/dev/null || true
    done < <(find "$cg" -mindepth 1 -depth -type d 2>/dev/null)
}
_cg_kill_all() {
    # 可靠地杀死 cgroup 内所有进程，确保 destroy 时不残留。
    # 流程: freeze → cgroup.kill → 等待清空 → kill -9 兜底 → 迁回根 cgroup
    local name="$1" cg procs waited
    cg=$(_cg "$name")
    [ -d "$cg" ] || return 0

    # 1. Freeze — 原子冻结 cgroup 内所有进程
    if [ -f "$cg/cgroup.freeze" ]; then
        echo 1 > "$cg/cgroup.freeze" 2>/dev/null || true
        waited=0
        while [ $waited -lt 20 ]; do
            local frozen
            frozen=$(grep "^frozen " "$cg/cgroup.events" 2>/dev/null | awk '{print $2}')
            [ "$frozen" = "1" ] && break
            sleep 0.1
            waited=$((waited + 1))
        done
    fi

    # 2. Kill — 内核向 cgroup 内所有进程发 SIGKILL，无竞态
    if [ -f "$cg/cgroup.kill" ]; then
        echo 1 > "$cg/cgroup.kill" 2>/dev/null || true
        waited=0
        while [ $waited -lt 50 ]; do
            [ "$(_cg_is_populated "$name")" = "0" ] && break
            sleep 0.1
            waited=$((waited + 1))
        done
    fi

    # 3. 兜底: kill -9 残留进程（cgroup.kill 可能因内核版本问题未生效）
    procs=$(_cg_all_procs "$name")
    for p in $procs; do
        kill -9 "$p" 2>/dev/null || true
    done
    # 再等一等让进程被 reaped
    waited=0
    while [ $waited -lt 20 ]; do
        [ "$(_cg_is_populated "$name")" = "0" ] && break
        sleep 0.1
        waited=$((waited + 1))
    done

    # 4. Fallback: 如果有残留，迁回根 cgroup 以免 rmdir 失败
    procs=$(_cg_all_procs "$name")
    for p in $procs; do
        echo "$p" > "$(_cg_root_procs)" 2>/dev/null || true
    done
    # 再次检查，顽固僵尸进程忽略（内核会自动清理）
    sleep 0.3
}
_cg_set_cpu() {
    local name="$1" cores="$2"
    [ "$cores" != "0" ] && [ -n "$cores" ] \
        && echo "$(( cores * 100000 )) 100000" > "$(_cg_cpu "$name")" || true
}
_cg_set_mem() {
    local name="$1" bytes="$2"
    [ "$bytes" != "0" ] && [ -n "$bytes" ] \
        && echo "$bytes" > "$(_cg_mem "$name")" \
        && echo 0 > "$(_cg_swap "$name")" 2>/dev/null || true
}

# ==================================================================
# 工具函数
# ==================================================================

parse_mem() {
    local raw="$1"
    [ "$raw" = "0" ] && { echo "0"; return; }
    [ -z "$raw" ] && { echo "0"; return; }
    local num="${raw//[^0-9]/}" unit="${raw//[0-9]/}"
    case "$unit" in
        K|k) echo $(( num * 1024 )) ;;
        M|m) echo $(( num * 1024 * 1024 )) ;;
        G|g) echo $(( num * 1024 * 1024 * 1024 )) ;;
        *)   die "无法识别的内存单位: $raw (支持 K/M/G)" ;;
    esac
}

parse_device() {
    local dev="$1"
    if [[ "$dev" == *:* ]]; then
        echo "${dev%%:*}" "${dev##*:}"
    else
        echo "$dev" "*"
    fi
}

_cg_id() {
    stat -c %i "$(_cg "$1")" 2>/dev/null
}

# 清理 eBPF 设备预留条目（即使 cgroup 目录已消失也必须执行）
_cleanup_device_entries() {
    local name="$1" cgid="$2"
    local device_file="/tmp/neu_box_devices_${name}"
    local cgid_file="/tmp/neu_box_cgid_${name}"
    if [ -z "$cgid" ] && [ -f "$cgid_file" ]; then
        cgid=$(cat "$cgid_file" 2>/dev/null || true)
    fi
    if [ ! -f "$device_file" ]; then
        rm -f "$cgid_file"
        return 0
    fi
    local seen_majors="" failed=0
    while read dev; do
        [ -z "$dev" ] && continue
        local major minor; read -r major minor <<< "$(parse_device "$dev")"
        local minor_num
        if [ "$minor" = "*" ]; then minor_num=4294967295
        else minor_num=$minor; fi
        # 精确设备条目（不依赖 cgid）
        local key_hex; key_hex=$(printf '%02x %02x %02x %02x %02x %02x %02x %02x' \
            $(( major & 0xFF )) $(( (major >> 8) & 0xFF )) 0 0 \
            $(( minor_num & 0xFF )) $(( (minor_num >> 8) & 0xFF )) 0 0)
        if [ -e "$MAP_RESERVED_PIN" ]; then
            if ! bpftool map show pinned "$MAP_RESERVED_PIN" >/dev/null 2>&1; then
                failed=1
            elif bpftool map lookup pinned "$MAP_RESERVED_PIN" \
                key hex $key_hex >/dev/null 2>&1; then
                # 只有确认 key 存在时才删除；删除失败必须保留元数据重试。
                bpftool map delete pinned "$MAP_RESERVED_PIN" \
                    key hex $key_hex 2>/dev/null || failed=1
            fi
        fi
        # major 条目（需要 cgid，无 cgid 时跳过但不影响设备释放）
        if [ -n "$cgid" ] && [[ " $seen_majors " != *" $major "* ]]; then
            local major_key
            major_key="$(_u64_hex "$cgid") $(_u32_hex "$major") 00 00 00 00"
            if [ -e "$MAP_MAJORS_PIN" ]; then
                if ! bpftool map show pinned "$MAP_MAJORS_PIN" >/dev/null 2>&1; then
                    failed=1
                elif bpftool map lookup pinned "$MAP_MAJORS_PIN" \
                    key hex $major_key >/dev/null 2>&1; then
                    bpftool map delete pinned "$MAP_MAJORS_PIN" \
                        key hex $major_key 2>/dev/null || failed=1
                fi
            fi
            seen_majors="$seen_majors $major"
        fi
    done < "$device_file"
    if [ "$failed" -ne 0 ]; then
        echo "错误: eBPF 设备预留清理失败，保留 $device_file 供重试" >&2
        return 1
    fi
    rm -f "$device_file" "$cgid_file"
}

# 自动检测 bpftool 支持的 cgroup device attach type
# 旧版用 cgroup_device，新版（5.15+）用 device
_bpf_device_attach_type() {
    if bpftool cgroup help 2>&1 | grep -q 'cgroup_device'; then
        echo "cgroup_device"
    else
        echo "device"
    fi
}
BPF_DEV_TYPE="$(_bpf_device_attach_type)"

# little-endian hex 构建
_u64_hex() {
    local v="$1"
    printf '%02x %02x %02x %02x %02x %02x %02x %02x' \
        $(( v & 0xFF )) $(( (v >> 8) & 0xFF )) \
        $(( (v >> 16) & 0xFF )) $(( (v >> 24) & 0xFF )) \
        $(( (v >> 32) & 0xFF )) $(( (v >> 40) & 0xFF )) \
        $(( (v >> 48) & 0xFF )) $(( (v >> 56) & 0xFF ))
}

_u32_hex() {
    local v="$1"
    printf '%02x %02x %02x %02x' \
        $(( v & 0xFF )) $(( (v >> 8) & 0xFF )) \
        $(( (v >> 16) & 0xFF )) $(( (v >> 24) & 0xFF ))
}

# 从 root cgroup 分离所有 device_reserve BPF 程序
_bpf_detach() {
    echo "[bpf] 分离 root cgroup 上的 BPF 程序 ..."
    bpftool cgroup show "$CGROUP_ROOT" 2>/dev/null | \
        awk '/device_reserve/{print $1}' | \
        while read id; do
            bpftool cgroup detach "$CGROUP_ROOT" $BPF_DEV_TYPE id "$id" 2>/dev/null || true
        done
    echo "[bpf] ✓ 已分离"
}

_bpf_unload() {
    echo "[bpf] 卸载 BPF 程序及 maps ..."
    rm -rf "$BPF_PIN" "$MAP_DIR" 2>/dev/null || true
    echo "[bpf] ✓ 已卸载"
}

_bpf_is_loaded() {
    [ -f "$BPF_PIN" ] && [ -f "$MAP_RESERVED_PIN" ] && [ -f "$MAP_MAJORS_PIN" ]
}

_bpf_is_attached() {
    bpftool cgroup show "$CGROUP_ROOT" 2>/dev/null | grep -q "device_reserve"
}

# 确保 BPF 程序已加载并挂载到 root cgroup（仅首次执行，后续 create 跳过）
_ensure_bpf_ready() {
    # 已加载且已挂载 → 直接返回
    if _bpf_is_loaded && _bpf_is_attached; then
        echo "[bpf] BPF 程序已就绪，跳过加载"
        return
    fi

    # 已加载但未挂载 → 仅重新挂载
    if _bpf_is_loaded && ! _bpf_is_attached; then
        echo "[bpf] BPF 程序已加载但未挂载，重新挂载..."
        bpftool cgroup attach "$CGROUP_ROOT" $BPF_DEV_TYPE \
            pinned "$BPF_PIN" multi || die "BPF 挂载到 root cgroup 失败"
        echo "[bpf] ✓ 已挂载"
        return
    fi

    # 首次加载：使用发布阶段预编译的对象，目标节点不需要 clang。
    [ -f "$BPF_OBJ" ] || die "缺少预编译 BPF 对象: $BPF_OBJ"
    echo "[bpf] 首次加载预编译对象 $BPF_OBJ ..."

    echo "[bpf] 加载 BPF 程序 ..."
    mkdir -p "$MAP_DIR"
    bpftool prog load "$BPF_OBJ" "$BPF_PIN" pinmaps "$MAP_DIR"
    echo "[bpf] ✓ 已加载，maps:"
    ls -l "$MAP_DIR/"

    echo "[bpf] 挂载 BPF 到 root cgroup ..."
    bpftool cgroup attach "$CGROUP_ROOT" $BPF_DEV_TYPE \
        pinned "$BPF_PIN" multi || die "BPF 挂载到 root cgroup 失败"
    echo "[bpf] ✓ 已挂载"
}

# ==================================================================
# 命令
# ==================================================================

cmd_load() {
    [ -f "$BPF_OBJ" ] || die "发布包缺少预编译 BPF 对象: $BPF_OBJ"
    rm -rf "$BPF_PIN" "$MAP_DIR" 2>/dev/null || true
    mkdir -p "$MAP_DIR"
    bpftool prog load "$BPF_OBJ" "$BPF_PIN" pinmaps "$MAP_DIR"
    echo "✓ BPF 程序已加载"
    ls -l "$MAP_DIR/"
    bpftool cgroup attach "$CGROUP_ROOT" $BPF_DEV_TYPE \
        pinned "$BPF_PIN" multi || die "BPF 挂载失败"
    echo "✓ 已挂载到 root cgroup"
}

cmd_create() {
    _require_root "create <name> <cpu> <mem> [devices...]"
    local name="$1"; [ -n "$name" ] || die "用法: $0 create <name> <cpu> <mem> [major:minor ...]"
    local cpu="$2";  [ -n "$cpu"  ] || die "用法: $0 create <name> <cpu> <mem> [major:minor ...]"
    local mem="$3";  [ -n "$mem"  ] || die "用法: $0 create <name> <cpu> <mem> [major:minor ...]"
    shift 3

    local mem_bytes; mem_bytes=$(parse_mem "$mem")
    [ ! -d "$(_cg "$name")" ] || die "沙盒 '${name}' 已存在"

    echo "cgroup 版本: v${CGROUP_VER}"

    # 先注册 systemd slice（Delegate=true），systemd 自动创建 cgroup 目录
    _cg_enable
    _register_systemd_slice "$name"
    _cg_mkdir "$name"  # 补齐 systemd 未创建的目录
    _cg_set_cpu "$name" "$cpu"
    _cg_set_mem "$name" "$mem_bytes"

    # 获取 cgroup ID（内核级唯一标识，与 bpf_get_current_cgroup_id 对应）
    local cgid; cgid=$(_cg_id "$name")
    [ -n "$cgid" ] || die "无法获取 cgroup ID"
    echo "$cgid" > "/tmp/neu_box_cgid_${name}"

    [ "$cpu" != "0" ] && echo "  CPU: ${cpu} 核"     || echo "  CPU: 不限"
    [ "$mem_bytes" != "0" ] && echo "  内存: ${mem}" || echo "  内存: 不限"
    echo "  cgroup ID: $cgid"

    # eBPF 设备预留（挂在 root cgroup，全局生效）
    _ensure_bpf_ready

    local device_file="/tmp/neu_box_devices_${name}"
    rm -f "$device_file"

    if [ $# -gt 0 ]; then
        for dev in "$@"; do
            local major minor; read -r major minor <<< "$(parse_device "$dev")"
            local minor_num
            if [ "$minor" = "*" ]; then
                minor_num=4294967295  # 0xFFFFFFFF
            else
                minor_num=$minor
            fi
            
            # 使用标准的 _u32_hex 函数，自动处理好 4字节 Little-Endian
            local key_hex val_hex
            key_hex="$(_u32_hex "$major") $(_u32_hex "$minor_num")"
            val_hex=$(_u64_hex "$cgid")
            
            bpftool map update pinned "$MAP_RESERVED_PIN" \
                key hex $key_hex value hex $val_hex \
                || die "设备预留失败: $dev"
            echo "$dev" >> "$device_file"
        done
        echo "  独占设备: $*"
        # 标记该 cgroup 在对应 major 有预留
        # 去重: 只记录每个 major 一次
        local seen_majors=""
        for dev in "$@"; do
            local mj mn; read -r mj mn <<< "$(parse_device "$dev")"
            if [[ " $seen_majors " != *" $mj "* ]]; then
                bpftool map update pinned "$MAP_MAJORS_PIN" \
                    key hex $(_u64_hex "$cgid") $(_u32_hex "$mj") 00 00 00 00 value hex 01 \
                    || die "标记 reserved_majors 失败 (major=$mj)"
                seen_majors="$seen_majors $mj"
            fi
        done
    else
        echo "  设备: 全共享（未预留任何设备）"
    fi

    echo "✓ 沙盒已创建"
    echo "  加入进程: sudo $0 join $name <PID>"
}

cmd_join() {
    _require_root "join <name> <PID>"
    local name="$1" pid="$2"
    [ -n "$name" ] || die "用法: $0 join <name> <PID>"
    [ -n "$pid"  ] || die "用法: $0 join <name> <PID>"
    [ -d "/proc/$pid" ] || die "PID $pid 不存在"
    [ -d "$(_cg "$name")" ] || die "沙盒 '${name}' 不存在"

    _cg_join "$name" "$pid"
    echo "✓ PID $pid 已加入沙盒 '${name}'"
}

cmd_status() {
    local name="$1"; [ -n "$name" ] || die "用法: $0 status <name>"
    [ -d "$(_cg "$name")" ] || die "沙盒 '${name}' 不存在"

    echo "=== 沙盒: $name ==="
    echo "cgroup: v${CGROUP_VER}"
    echo ""

    echo "--- CPU ---"
    cat "$(_cg_cpu "$name")" 2>/dev/null || echo '-'
    echo ""

    echo "--- 内存 ---"
    local limit; limit=$(cat "$(_cg_mem "$name")" 2>/dev/null || echo '-')
    case "$limit" in
        9223372036854771712|-1|max) echo "  limit:  不限" ;;
        *) echo "  limit:  $limit bytes ($(( limit / 1024 / 1024 ))M)" ;;
    esac
    echo "  usage:  $(cat "$(_cg_memcur "$name")" 2>/dev/null || echo '-') bytes"
    echo ""

    echo "--- 设备预留 ---"
    echo "  [reserved_devices]"
    bpftool map dump pinned "$MAP_RESERVED_PIN" 2>/dev/null || echo "    (无)"
    echo "  [reserving_cgroups]"
    bpftool map dump pinned "$MAP_MAJORS_PIN" 2>/dev/null || echo "    (无)"
    echo ""

    echo "--- 进程列表 ---"
    local procs; procs=$(cat "$(_cg_procs "$name")" 2>/dev/null)
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

cmd_destroy() {
    _require_root "destroy <name>"
    local name="$1"; [ -n "$name" ] || die "用法: $0 destroy <name>"

    # 先拿 cgid（必须在删目录之前）。cgroup 可能已不存在（进程退出后内核
    # 自动删除空 cgroup），此时必须置空继续，否则 set -e 会让脚本静默退出，
    # 后面的 eBPF / 元数据清理永远执行不到。
    local cgid; cgid=$(_cg_id "$name") || cgid=""

    if [ -d "$(_cg "$name")" ]; then
        _unregister_systemd_slice "$name"
        _cg_kickall "$name"
        _cg_remove_children "$name"
        _cg_rmdir "$name"
    fi

    # 清理 eBPF map 条目（即使 cgroup 已消失也必须执行）
    _cleanup_device_entries "$name" "${cgid:-}"
    echo "✓ 沙盒 '${name}' 已销毁"
}

cmd_list() {
    local found=false
    for d in "${CGROUP_ROOT}"/${PREFIX}*/; do
        [ -d "$d" ] || continue
        found=true
        local name="${d##*/${PREFIX}}"
        name="${name%/}"
        echo "$name"
    done
    if ! $found; then
        echo "(无)"
    fi
}

cmd_cleanup() {
    _require_root "cleanup"
    echo "=== 完全清理 ==="
    local failed=0 total=0

    for d in "${CGROUP_ROOT}"/${PREFIX}*/; do
        [ -d "$d" ] || continue
        total=$((total + 1))
        local name="${d##*/${PREFIX}}"
        name="${name%/}"
        echo -n "销毁沙盒: $name ... "

        # 0. 注销 systemd slice
        _unregister_systemd_slice "$name"

        # 1. 杀死 cgroup 内所有进程
        _cg_kickall "$name" 2>/dev/null || true

        # 2. 递归删除子 cgroup（最深优先），以防沙盒内进程创建了子 cgroup
        find "$d" -mindepth 1 -type d 2>/dev/null | sort -r | while read child; do
            # 先把子 cgroup 里的进程迁走/杀掉
            if [ -f "$child/cgroup.kill" ]; then
                echo 1 > "$child/cgroup.kill" 2>/dev/null || true
                sleep 0.3
            fi
            rmdir "$child" 2>/dev/null || true
        done

        # 3. 再次确保主 cgroup 无进程
        local procs
        procs=$(cat "$d/cgroup.procs" 2>/dev/null || true)
        for p in $procs; do
            echo "$p" > "$(_cg_root_procs)" 2>/dev/null || true
        done
        sleep 0.2

        # 4. 清 eBPF map 条目
        local cgid; cgid=$(stat -c %i "$d" 2>/dev/null)
        _cleanup_device_entries "$name" "${cgid:-}"

        # 5. 删除主 cgroup 目录
        if rmdir "$d" 2>/dev/null; then
            echo "✓"
        else
            echo "✗ 失败（目录非空或无权限）"
            failed=$((failed + 1))
        fi
    done

    # 分离并卸载 BPF 程序
    _bpf_detach
    _bpf_unload

    rm -f /tmp/neu_box_devices_* /tmp/neu_box_cgid_* 2>/dev/null || true

    # ===== 验证清理结果 =====
    local remaining=0
    for d in "${CGROUP_ROOT}"/${PREFIX}*/; do
        [ -d "$d" ] || continue
        remaining=$((remaining + 1))
        echo "⚠ 残留: ${d}"
    done

    if [ $remaining -eq 0 ]; then
        echo "✓ 清理完成（${total} 个沙盒已销毁）"
    else
        echo "⚠ 清理结束：${remaining}/${total} 个沙盒未能删除"
        echo "  请检查是否缺少 root 权限，或手动执行: sudo $0 cleanup"
    fi
}

# ==================================================================
# main
# ==================================================================

case "${1:-}" in
    load)    cmd_load ;;
    create)  shift; cmd_create "$@" ;;
    join)    cmd_join    "${2:-}" "${3:-}" ;;
    status)  cmd_status  "${2:-}" ;;
    destroy) cmd_destroy "${2:-}" ;;
    list)    cmd_list ;;
    cleanup) cmd_cleanup ;;
    *)
        echo "cgroup 版本: v${CGROUP_VER}"
        echo ""
        echo "用法: $0 {create|join|status|destroy|list|cleanup} [args...]"
        echo ""
        echo "  create <name> <cpu> <mem> [major:minor ...]"
        echo "          cpu  = CPU 核数 (0=不限)"
        echo "          mem  = 内存 (512M / 2G / 0=不限)"
        echo "          device = 设备号 (如 235:0 / 235:* / 235)"
        echo "  join    <name> <PID>"
        echo "  status  <name>"
        echo "  destroy <name>"
        echo "  list"
        echo "  cleanup"
        ;;
esac
