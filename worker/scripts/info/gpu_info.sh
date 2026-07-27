#!/bin/bash
# ============================================================
# 脚本: gpu_info.sh
# 用途: 采集 NVIDIA GPU 设备信息
#
# 用 /sys/class/drm/card* 建立 minor→PCI 映射（只取 NVIDIA），
# 然后与 nvidia-smi 的 PCI bus ID 匹配。
# 这样 minor 号不受 nvidia-smi index 重新编号影响。
#
# 空闲判定:
#   1. GPU 利用率 = 0%
#   2. 显存使用 ≤ IDLE_MEM_MB（默认 200MB）
#   3. nvidia-smi 不可见 → eBPF 已分配 → busy
# ============================================================

IDLE_MEM_MB=${IDLE_MEM_MB:-200}

if ! command -v nvidia-smi &>/dev/null; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

# ── 从 DRM 建立 NVIDIA minor→PCI 映射 ──
declare -A minor_to_pci
all_minors=()
for card in /sys/class/drm/card[0-9]*; do
    [ -L "$card" ] || continue
    dev_path=$(readlink -f "$card/device" 2>/dev/null)
    [ -n "$dev_path" ] || continue
    vendor=$(cat "$dev_path/vendor" 2>/dev/null)
    [ "$vendor" = "0x10de" ] || continue  # 只取 NVIDIA
    minor=$(echo "$(cat "$card/dev" 2>/dev/null)" | cut -d: -f2)
    [ -z "$minor" ] && continue
    bus_id=$(basename "$dev_path" | tr '[:upper:]' '[:lower:]')
    bus_id="${bus_id#0000:}"  # strip domain prefix
    minor_to_pci["$minor"]="$bus_id"
    all_minors+=("$minor")
done

total=${#all_minors[@]}
if [ "$total" -eq 0 ]; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

# ── nvidia-smi 获取可见 GPU 状态 ──
output=$(nvidia-smi --query-gpu=pci.bus_id,memory.total,memory.used,utilization.gpu \
    --format=csv,noheader,nounits 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$output" ]; then
    output=""
fi

# 默认：所有卡标记为 busy（nvidia-smi 不可见也是 busy）
declare -A minor_busy
for minor in "${all_minors[@]}"; do
    minor_busy["$minor"]=1
done

# 匹配 nvidia-smi 可见的卡，判定是否空闲
while IFS=, read -r pci_bus mem_total mem_used gpu_util; do
    pci_bus=$(echo "$pci_bus" | xargs | tr '[:upper:]' '[:lower:]')
    pci_bus="${pci_bus#00000000:}"  # strip 8-zero domain (nvidia-smi actual format)
    pci_bus="${pci_bus#0000:}"      # strip 4-zero domain (DRM format, fallback)
    mem_used=$(echo "$mem_used" | xargs)
    gpu_util=$(echo "$gpu_util" | xargs)

    # 判定：利用率=0 且 显存≤阈值 → 空闲
    is_idle=0
    if [ "$gpu_util" = "0" ] || [ -z "$gpu_util" ]; then
        if [ "$mem_used" -le "$IDLE_MEM_MB" ] 2>/dev/null; then
            is_idle=1
        fi
    fi

    # 找所有对应这个 PCI bus 的 minor 号，全部标记
    for m in "${all_minors[@]}"; do
        if [ "${minor_to_pci[$m]}" = "$pci_bus" ]; then
            minor_busy["$m"]=$((1 - is_idle))
        fi
    done
done <<< "$output"

# ── 输出 ──
idle=0
busy_json="["
first=1
for minor in $(printf '%s\n' "${all_minors[@]}" | sort -n); do
    if [ "${minor_busy[$minor]}" = "0" ]; then
        idle=$((idle + 1))
    else
        if [ "$first" -eq 1 ]; then first=0; else busy_json+=","; fi
        busy_json+="$minor"
    fi
done
busy_json+="]"

echo "{\"total\":$total,\"idle\":$idle,\"busy_ids\":$busy_json}"