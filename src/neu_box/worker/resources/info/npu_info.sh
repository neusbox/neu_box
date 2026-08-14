#!/bin/bash
# Ascend NPU 状态采集。
#
# 解析 npu-smi info 的设备表和进程表，输出与 gpu_info.sh 相同的协议：
# {"total":N,"idle":N,"busy_ids":[minor,...]}
# busy_ids 中的 NPU ID 对应 /dev/davinci<NPU ID> 的 minor。

if ! command -v npu-smi &>/dev/null; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

output=$(npu-smi info 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$output" ]; then
    echo '{"total":0,"idle":0,"busy_ids":[]}'
    exit 0
fi

# 设备表中每颗芯片占两行，第二行是使用率信息。
total=0
in_table=0
row_idx=0
while IFS= read -r line; do
    if [[ "$line" =~ ^\+=== ]]; then
        in_table=1
        continue
    fi
    [ "$in_table" -eq 0 ] && continue
    [[ "$line" =~ ^\+--- ]] && break
    [[ "$line" =~ ^\| ]] || continue
    [[ "$line" =~ NPU ]] && continue
    [[ "$line" =~ Name ]] && continue
    [[ "$line" =~ Chip ]] && continue
    if [ $((row_idx % 2)) -eq 1 ]; then
        total=$((total + 1))
    fi
    row_idx=$((row_idx + 1))
done <<< "$output"

# 进程表第一列是 NPU ID；有运行进程的 NPU 视为忙碌。
declare -A busy_npus
in_process_table=0
while IFS= read -r line; do
    if [[ "$line" =~ "Process id" ]] && [[ "$line" =~ "Process name" ]]; then
        in_process_table=1
        continue
    fi
    [ "$in_process_table" -eq 0 ] && continue
    [[ "$line" =~ "No running processes" ]] && continue
    [[ "$line" =~ ^\+ ]] && continue
    if [[ "$line" =~ ^\| ]]; then
        first_column=$(echo "$line" | cut -d'|' -f2 | xargs)
        npu_id=$(echo "$first_column" | awk '{print $1}')
        if [[ "$npu_id" =~ ^[0-9]+$ ]]; then
            busy_npus["$npu_id"]=1
        fi
    fi
done <<< "$output"

idle=0
busy_json="["
first=1
for ((i=0; i<total; i++)); do
    if [ -z "${busy_npus[$i]}" ]; then
        idle=$((idle + 1))
    else
        if [ "$first" -eq 1 ]; then
            first=0
        else
            busy_json+=","
        fi
        busy_json+="$i"
    fi
done
busy_json+="]"

echo "{\"total\":$total,\"idle\":$idle,\"busy_ids\":$busy_json}"
