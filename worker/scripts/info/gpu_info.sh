#!/bin/bash
# ============================================================
# 脚本: gpu_info.sh
# 用途: 采集 NVIDIA GPU 设备信息
#
# busy_ids 使用真实 minor 号（Device Minor），不受 eBPF 隐藏 GPU
# 后 nvidia-smi 重新编号的影响。
#
# 空闲判定（两个条件都满足才算空闲）:
#   1. GPU 利用率 = 0%
#   2. 显存使用 ≤ IDLE_MEM_MB（默认 200MB，env 可配）
#   3. /dev/nvidia* 不存在 → 视为已被 eBPF 分配，非空闲
# ============================================================

IDLE_MEM_MB=${IDLE_MEM_MB:-200}

if ! command -v nvidia-smi &>/dev/null; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

# ── 建立 /dev/nvidia* 的 minor→pci 映射和 minor 集合 ──
declare -A minor_to_pci
declare -a all_minors
for dev in /dev/nvidia[0-9]*; do
    [ -e "$dev" ] || continue
    minor=$(stat -c '%T' "$dev" 2>/dev/null)
    [ -z "$minor" ] && continue
    minor=$((16#$minor))  # hex → dec
    # 获取 PCI bus ID: /sys/dev/char/<major>:<minor>/device → 读取 bus ID
    major=$(stat -c '%t' "$dev" 2>/dev/null)
    [ -z "$major" ] && continue
    major=$((16#$major))
    bus_id=""
    dev_path="/sys/dev/char/${major}:${minor}/device"
    if [ -L "$dev_path" ]; then
        # 类似 0000:01:00.0 → 标准化为 BUS_ID
        bus_id=$(readlink "$dev_path" 2>/dev/null | xargs basename)
        # 去掉 0000: 前缀（不同系统可能不同）
        bus_id="${bus_id#0000:}"
    fi
    [ -n "$bus_id" ] && minor_to_pci["$minor"]="$bus_id"
    all_minors+=("$minor")
done

# ── 从 nvidia-smi 获取可见 GPU 的 busy 状态（按 PCI bus ID 匹配） ──
output=$(nvidia-smi --query-gpu=pci.bus_id,memory.total,memory.used,utilization.gpu \
    --format=csv,noheader,nounits 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$output" ]; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

total=${#all_minors[@]}
# nvidia-smi 不可见的卡视为已被分配（eBPF 占用）
declare -A minor_busy
for minor in "${all_minors[@]}"; do
    if [ -z "${minor_to_pci[$minor]}" ]; then
        minor_busy["$minor"]=1  # 找不到 PCI 映射 → 视为占用
    else
        minor_busy["$minor"]=1  # 默认占用，nvidia-smi 确认空闲后再改
    fi
done

while IFS=, read -r pci_bus mem_total mem_used gpu_util; do
    pci_bus=$(echo "$pci_bus" | xargs)
    mem_used=$(echo "$mem_used" | xargs)
    gpu_util=$(echo "$gpu_util" | xargs)
    # 去除 0000: 前缀
    pci_bus="${pci_bus#0000:}"

    # 找对应的 minor 号
    minor=""
    for m in "${all_minors[@]}"; do
        if [ "${minor_to_pci[$m]}" = "$pci_bus" ]; then
            minor="$m"
            break
        fi
    done
    [ -z "$minor" ] && continue

    # 判定是否空闲
    busy=1
    if [ "$gpu_util" = "0" ] || [ -z "$gpu_util" ]; then
        if [ "$mem_used" -le "$IDLE_MEM_MB" ] 2>/dev/null; then
            busy=0
        fi
    fi
    minor_busy["$minor"]=$busy
done <<< "$output"

# ── 输出 ──
idle=0
busy_json="["
first=1
for minor in $(printf '%s\n' "${all_minors[@]}" | sort -n); do
    val=${minor_busy["$minor"]}
    if [ "$val" = "0" ]; then
        idle=$((idle + 1))
    else
        if [ "$first" -eq 1 ]; then first=0; else busy_json+=","; fi
        busy_json+="$minor"
    fi
done
busy_json+="]"

echo "{\"total\":$total,\"idle\":$idle,\"busy_ids\":$busy_json}"
