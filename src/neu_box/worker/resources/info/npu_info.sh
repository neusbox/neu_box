#!/bin/bash
# Ascend NPU 状态采集。
#
# 解析 npu-smi info 的设备表和进程表，输出与 gpu_info.sh 相同的协议：
# {"total":N,"idle":N,"busy_ids":[minor,...]}
# busy_ids 中的 NPU ID 对应 /dev/davinci<NPU ID> 的 minor。
#
# 设备 cgroup/eBPF 会使已经分配的卡从 npu-smi 输出中消失，因此不能用
# npu-smi 当前可见的行数推导物理卡总数。以 /dev/davinciN 为完整清单；
# 清单中未出现在 npu-smi 设备表里的卡保持 busy（fail-closed）。

NPU_DEVICE_ROOT=${NPU_DEVICE_ROOT:-/dev}

if ! command -v npu-smi &>/dev/null; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

output=$(npu-smi info 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$output" ]; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

# 收集完整设备清单。NPU_DEVICE_ROOT 仅用于测试或特殊挂载布局；生产默认 /dev。
declare -A all_npus
for device in "$NPU_DEVICE_ROOT"/davinci[0-9]*; do
    [ -e "$device" ] || continue
    name=${device##*/}
    if [[ "$name" =~ ^davinci([0-9]+)$ ]]; then
        all_npus["$((10#${BASH_REMATCH[1]}))"]=1
    fi
done

# 设备表中每颗芯片占两行，第一行的首列是 NPU ID。
declare -A visible_npus
in_device_table=0
row_idx=0
while IFS= read -r line; do
    if [[ "$line" =~ Process[[:space:]]+id ]] \
        && [[ "$line" =~ Process[[:space:]]+name ]]; then
        in_device_table=0
        continue
    fi
    if [[ "$line" =~ ^\| ]] && [[ "$line" =~ NPU ]] \
        && [[ "$line" =~ Name ]] && [[ ! "$line" =~ Process ]]; then
        in_device_table=1
        row_idx=0
        continue
    fi
    [ "$in_device_table" -eq 0 ] && continue
    [[ "$line" =~ ^\+ ]] && continue
    [[ "$line" =~ ^\| ]] || continue
    [[ "$line" =~ NPU ]] && continue
    [[ "$line" =~ Name ]] && continue
    [[ "$line" =~ Chip ]] && continue
    if [ $((row_idx % 2)) -eq 0 ]; then
        first_column=${line#|}
        first_column=${first_column%%|*}
        read -r npu_id _unused <<< "$first_column"
        if [[ "$npu_id" =~ ^[0-9]+$ ]]; then
            visible_npus["$((10#$npu_id))"]=1
        fi
    fi
    row_idx=$((row_idx + 1))
done <<< "$output"

# 进程表第一列是 NPU ID；有运行进程的 NPU 视为忙碌。
declare -A busy_npus
in_process_table=0
while IFS= read -r line; do
    if [[ "$line" =~ Process[[:space:]]+id ]] \
        && [[ "$line" =~ Process[[:space:]]+name ]]; then
        in_process_table=1
        continue
    fi
    [ "$in_process_table" -eq 0 ] && continue
    [[ "$line" =~ "No running processes" ]] && continue
    [[ "$line" =~ ^\+ ]] && continue
    if [[ "$line" =~ ^\| ]]; then
        first_column=${line#|}
        first_column=${first_column%%|*}
        read -r npu_id _unused <<< "$first_column"
        if [[ "$npu_id" =~ ^[0-9]+$ ]]; then
            busy_npus["$((10#$npu_id))"]=1
        fi
    fi
done <<< "$output"

# 兼容没有 /dev/davinciN 节点的旧环境：至少保留 npu-smi 实际报告的 ID，
# 但不再把卡号错误地压缩为 0..count-1。
if [ "${#all_npus[@]}" -eq 0 ]; then
    for npu_id in "${!visible_npus[@]}" "${!busy_npus[@]}"; do
        [ -n "$npu_id" ] && all_npus["$npu_id"]=1
    done
fi

total=${#all_npus[@]}
idle=0
busy_json="["
first=1
while IFS= read -r npu_id; do
    [ -n "$npu_id" ] || continue
    if [ -n "${visible_npus[$npu_id]:-}" ] \
        && [ -z "${busy_npus[$npu_id]:-}" ]; then
        idle=$((idle + 1))
    else
        if [ "$first" -eq 1 ]; then
            first=0
        else
            busy_json+=","
        fi
        busy_json+="$npu_id"
    fi
done < <(printf '%s\n' "${!all_npus[@]}" | sort -n)
busy_json+="]"

echo "{\"total\":$total,\"idle\":$idle,\"busy_ids\":$busy_json}"
