#!/bin/bash
# NVIDIA GPU 状态采集。
#
# 用 NVIDIA procfs 建立 device minor 与 PCI bus ID 的映射，再匹配
# nvidia-smi。procfs 在设备被 eBPF 隔离后仍包含完整物理卡清单。
# 被 neu_box eBPF 隔离而对 nvidia-smi 不可见的卡保持 busy。

IDLE_MEM_MB=${IDLE_MEM_MB:-200}

declare -A minor_to_pci
all_minors=()
for info in /proc/driver/nvidia/gpus/*/information; do
    [ -r "$info" ] || continue
    minor=$(awk '/^Device Minor:/ {print $3}' "$info")
    [ -n "$minor" ] || continue
    bus_id=$(basename "$(dirname "$info")" | tr '[:upper:]' '[:lower:]')
    bus_id="${bus_id#0000:}"
    minor_to_pci["$minor"]="$bus_id"
    all_minors+=("$minor")
done

total=${#all_minors[@]}
if [ "$total" -eq 0 ]; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

output=""
if command -v nvidia-smi &>/dev/null; then
    output=$(nvidia-smi \
        --query-gpu=pci.bus_id,memory.total,memory.used,utilization.gpu \
        --format=csv,noheader,nounits 2>/dev/null) || output=""
fi

declare -A minor_busy
for minor in "${all_minors[@]}"; do
    minor_busy["$minor"]=1
done

while IFS=, read -r pci_bus _mem_total mem_used gpu_util; do
    pci_bus=$(echo "$pci_bus" | xargs | tr '[:upper:]' '[:lower:]')
    pci_bus="${pci_bus#00000000:}"
    pci_bus="${pci_bus#0000:}"
    mem_used=$(echo "$mem_used" | xargs)
    gpu_util=$(echo "$gpu_util" | xargs)

    is_idle=0
    if [ "$gpu_util" = "0" ] || [ -z "$gpu_util" ]; then
        if [ "$mem_used" -le "$IDLE_MEM_MB" ] 2>/dev/null; then
            is_idle=1
        fi
    fi

    for minor in "${all_minors[@]}"; do
        if [ "${minor_to_pci[$minor]}" = "$pci_bus" ]; then
            minor_busy["$minor"]=$((1 - is_idle))
        fi
    done
done <<< "$output"

idle=0
busy_json="["
first=1
while read -r minor; do
    if [ "${minor_busy[$minor]}" = "0" ]; then
        idle=$((idle + 1))
    else
        if [ "$first" -eq 1 ]; then
            first=0
        else
            busy_json+=","
        fi
        busy_json+="$minor"
    fi
done < <(printf '%s\n' "${all_minors[@]}" | sort -n)
busy_json+="]"

echo "{\"total\":$total,\"idle\":$idle,\"busy_ids\":$busy_json}"
