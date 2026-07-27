#!/bin/bash
# neu-sbox — 终端沙盒隔离 / 命令任务提交 CLI
# 安装: worker 启动时自动复制到 /usr/local/bin/neu-sbox (chmod 755)

cmd="${1:-}"
WORKER_URL="${NEU_BOX_URL:-http://127.0.0.1:59075}"

case "$cmd" in
    acquire|a)
        # 全命名参数解析
        # neu-sbox acquire [--devices 1,3] [--device-num 2] [--cpu 4] [--mem 8] [--pid 12345] [--command "..."]
        device_ids=""
        device_num="0"
        cpu="0"
        memory="0"
        custom_pid=""
        command=""
        while [ $# -gt 1 ]; do
            case "$2" in
                --devices)    device_ids="$3"; shift 2 ;;
                --device-num) device_num="$3"; shift 2 ;;
                --cpu)        cpu="$3"; shift 2 ;;
                --mem)        memory="$3"; shift 2 ;;
                --pid)        custom_pid="$3"; shift 2 ;;
                --command)    command="$3"; shift 2 ;;
                *)
                    args+=("$2"); shift
                    ;;
            esac
        done
        # --devices 存在时忽略 --device-num（设备数从指定列表推断）
        [ -n "$device_ids" ] && device_num="0"
        # 向后兼容：位置参数回退
        [ "$device_num" = "0" ] && [ -z "$device_ids" ] && [ -n "${args[0]}" ] && device_num="${args[0]}"
        [ "$cpu" = "0" ] && [ -n "${args[1]}" ] && cpu="${args[1]}"
        [ "$memory" = "0" ] && [ -n "${args[2]}" ] && memory="${args[2]}"
        [ -z "$command" ] && [ -n "${args[3]}" ] && command="${args[3]}"

        # 构建设备 JSON 数组
        dev_ids_json="null"
        if [ -n "$device_ids" ]; then
            dev_ids_json="["
            IFS=',' read -ra parts <<< "$device_ids"
            first=1
            for d in "${parts[@]}"; do
                d=$(echo "$d" | xargs)
                [ -z "$d" ] && continue
                [ "$first" -eq 1 ] && first=0 || dev_ids_json+=","
                dev_ids_json+="\"${d}\""
            done
            dev_ids_json+="]"
        fi

        # 如果提供了命令参数，走任务提交路径
        if [ -n "$command" ]; then
            echo "[neu-sbox] 提交任务: device=${device_num} cpu=${cpu} mem=${memory}G"
            [ -n "$device_ids" ] && echo "[neu-sbox] 指定设备: ${device_ids}"
            echo "[neu-sbox] 命令: ${command}"
            echo "[neu-sbox] user=${USER}"

            resp=$(curl -s -X POST "${WORKER_URL}/command/run" \
                -H "Content-Type: application/json" \
                -d "{\"user_id\":\"${USER}\",\"command\":\"${command}\",\"device_num\":${device_num},\"device_ids\":${dev_ids_json},\"cpu\":${cpu},\"memory\":${memory},\"mem_unit\":\"GB\"}")

            echo "$resp" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "$resp"

            if echo "$resp" | grep -q '"task_id"'; then
                task_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])" 2>/dev/null)
                position=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['position'])" 2>/dev/null)
                echo ""
                echo "✓ 任务已提交，ID=${task_id} 队列位置 #${position}"
                echo "  查看日志: neu-sbox result ${task_id}"
            fi
            exit 0
        fi

        # 沙盒模式：--pid 指定进程，否则用当前 shell
        shell_pid="${custom_pid:-$PPID}"

        echo "[neu-sbox] 申请沙盒: device=${device_num} cpu=${cpu} mem=${memory}G"
        [ -n "$device_ids" ] && echo "[neu-sbox] 指定设备: ${device_ids}"
        echo "[neu-sbox] PID=${shell_pid} user=${USER}"

        resp=$(curl -s -X POST "${WORKER_URL}/sandbox/acquire" \
            -H "Content-Type: application/json" \
            -d "{\"username\":\"${USER}\",\"pid\":${shell_pid},\"device_num\":${device_num},\"device_ids\":${dev_ids_json},\"cpu\":${cpu},\"memory\":${memory},\"mem_unit\":\"GB\"}")

        echo "$resp" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "$resp"

        if echo "$resp" | grep -q '"sandbox_name"'; then
            name=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['sandbox_name'])" 2>/dev/null)
            echo ""
            echo "✓ 沙盒已创建，PID ${shell_pid} 独占设备。释放: neu-sbox release $name"
        fi
        ;;

    release|r)
        sandbox_name="${2:-}"
        if [ -z "$sandbox_name" ]; then
            echo "用法: neu-sbox release <sandbox_name>"
            echo "先用 neu-sbox list 查看你的沙盒"
            exit 1
        fi
        echo "[neu-sbox] 释放沙盒: ${sandbox_name}..."
        curl -s -X POST "${WORKER_URL}/sandbox/release" \
            -H "Content-Type: application/json" \
            -d "{\"sandbox_name\":\"${sandbox_name}\"}" \
            | python3 -m json.tool --no-ensure-ascii 2>/dev/null
        echo ""
        echo "✓ 沙盒已释放"
        ;;

    list|ls)
        echo "[neu-sbox] 所有沙盒:"
        python3 -c "
import json, sys
from urllib.request import urlopen

resp = urlopen('${WORKER_URL}/sandbox/list')
data = json.loads(resp.read())
sbs = data.get('sandboxes', [])

if not sbs:
    print('  (无)')
else:
    for s in sbs:
        name = s.get('name', '')
        owner = s.get('owner', '?')
        cpu = s.get('cpu', 0) or 0
        mem = s.get('mem', '0') or '0'
        devices = s.get('devices', [])
        dev_str = ','.join(str(d) for d in devices) if devices else '—'
        res_parts = []
        if cpu: res_parts.append(f'CPU={cpu}')
        if mem and mem != '0': res_parts.append(f'mem={mem}')
        res_str = ' '.join(res_parts) if res_parts else '资源不限'
        port_str = f' 端口={s[\"port\"]}' if s.get('port') else ''
        print(f'  {name}')
        print(f'    用户: {owner}  |  设备: {dev_str}  |  {res_str}{port_str}')
" 2>/dev/null || curl -s "${WORKER_URL}/sandbox/list?username=${USER}"
        ;;

    status|st)
        shell_pid=$PPID
        cgroup_path="/proc/${shell_pid}/cgroup"
        echo "[neu-sbox] Shell PID=${shell_pid}"
        if [ -r "$cgroup_path" ]; then
            grep -E "sandbox_" "$cgroup_path" 2>/dev/null || echo "  未在任何沙盒中"
        else
            echo "  无法读取 cgroup 信息"
        fi
        ;;

    join|j)
        sandbox_name="${2:-}"
        if [ -z "$sandbox_name" ]; then
            echo "用法: neu-sbox join <sandbox_name>"
            echo "先用 neu-sbox list 查看可用沙盒"
            exit 1
        fi
        shell_pid=$PPID
        echo "[neu-sbox] 加入沙盒: ${sandbox_name}"
        echo "[neu-sbox] PID=${shell_pid} user=${USER}"
        resp=$(curl -s -X POST "${WORKER_URL}/sandbox/join" \
            -H "Content-Type: application/json" \
            -d "{\"username\":\"${USER}\",\"pid\":${shell_pid},\"sandbox_name\":\"${sandbox_name}\"}")
        echo "$resp" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "$resp"
        if echo "$resp" | grep -q '"pid"'; then
            echo ""
            echo "✓ 已加入沙盒 ${sandbox_name}"
        fi
        ;;

    tasks|t)
        echo "[neu-sbox] 任务队列:"
        resp=$(curl -s "${WORKER_URL}/command/queue")
        echo "$resp" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || echo "$resp"
        ;;

    result|res|log|l)
        task_id="${2:-}"
        if [ -z "$task_id" ]; then
            echo "用法: neu-sbox result <task_id>"
            exit 1
        fi
        python3 -c "
import json, sys, time
from urllib.request import urlopen

url = '${WORKER_URL}'
tid = '${task_id}'

try:
    meta_resp = urlopen(f'{url}/command/result/{tid}')
    meta = json.loads(meta_resp.read())
except Exception as e:
    print(f'查询失败: {e}')
    sys.exit(1)

try:
    log_resp = urlopen(f'{url}/command/result/{tid}/log?raw=1')
    log_text = log_resp.read().decode('utf-8', errors='replace')
except Exception:
    log_text = ''

# ── 日志内容 ──
if log_text:
    print(log_text.rstrip())
else:
    print('(无输出)')

# ── 状态摘要 ──
r = meta.get('result', {}) or {}
rc = r.get('returncode', '')
status = meta.get('status', '?')
status_icon = {'completed': '✓', 'failed': '✗', 'running': '▶', 'queued': '○'}.get(status, '?')

line = f'[{status_icon} {status}]'
if rc is not None and rc != '':
    line += f'  rc={rc}'
    if r.get('timed_out'):
        line += ' (超时)'
line += f'  |  {meta.get(\"user_id\", \"?\")}  |  {meta.get(\"command\", \"?\")}'

cpu = meta.get('cpu', 0) or 0
mem = meta.get('mem', '0') or '0'
parts = []
if cpu: parts.append(f'CPU={cpu}')
if mem and mem != '0': parts.append(f'mem={mem}')
res_info = '  '.join(parts) if parts else '资源不限'
if res_info != '资源不限':
    line += f'  |  {res_info}'

dev_n = meta.get('device_num', 0) or 0
devices = meta.get('devices', [])
dev_str = ','.join(str(d) for d in devices) if devices else ''
if dev_n:
    line += f'  |  设备={dev_n}'
    if dev_str:
        line += f' ({dev_str})'

ts = meta.get('finished_at') or meta.get('created_at')
if ts:
    line += f'  |  {time.strftime(\"%m-%d %H:%M\", time.localtime(ts))}'

print()
print(line)
" 2>/dev/null || {
            echo "[neu-sbox] 查询失败，请检查 task_id 和网络连接"
        }
        ;;

    *)
        echo "neu-sbox — 终端沙盒隔离 / 命令任务提交"
        echo ""
        echo "用法: neu-sbox {acquire|release|list|status|join|tasks|result} [参数]"
        echo ""
        echo "  acquire [选项...]                        创建沙盒或提交任务"
        echo ""
        echo "  选项:"
        echo "    --devices 1,3    指定卡号 (逗号分隔，如 1,3,5)"
        echo "    --device-num 2   自动分配卡数量 (与 --devices 互斥)"
        echo "    --cpu 4          CPU 核数 (0=不限)"
        echo "    --mem 8          内存 GB (0=不限)"
        echo "    --pid 12345      为指定 PID 分配沙盒 (默认当前 shell)"
        echo "    --command \"...\"  提交命令任务 (不指定则为沙盒模式)"
        echo ""
        echo "  release <sandbox_name>                   释放沙盒"
        echo "  list                                     列出我的沙盒（含资源详情）"
        echo "  status                                   查看当前 shell 沙盒状态"
        echo "  join <sandbox_name>                      将当前 shell 加入已有沙盒（需归属校验）"
        echo "  tasks                                    查看任务队列"
        echo "  result <task_id>                         查看任务结果和日志"
        echo ""
        echo "示例:"
        echo "  neu-sbox acquire --devices 1                       # 申请卡 1"
        echo "  neu-sbox acquire --devices 1,3 --cpu 4 --mem 8      # 卡1,3 + 4核 + 8G"
        echo "  neu-sbox acquire --devices 1 --pid 12345            # 为 PID 12345 分配卡 1"
        echo "  neu-sbox acquire --devices 1 --cpu 4 --command \"npu-smi info\"  # 提交任务"
        echo "  neu-sbox list                            # 列出沙盒（显示设备/资源）"
        echo "  neu-sbox tasks                           # 查看队列"
        echo "  neu-sbox join sbx_pengyt_67890.slice      # 将当前 shell 加入已有沙盒"
        echo "  neu-sbox result abc123                   # 查看任务 abc123 结果和日志"
        echo "  neu-sbox release user_pengyt_12345"
        echo ""
        echo "已在沙盒中再次 acquire → 自动释放旧沙盒，覆盖为新资源"
        echo "远程 Worker: export NEU_BOX_URL=http://<worker_ip>:59075"
        echo ""
        echo "Docker 中使用沙盒:"
        echo "  沙盒的 eBPF 设备过滤只作用于沙盒 cgroup，Docker 容器默认使用自己的"
        echo "  cgroup，需要用 --cgroup-parent 让容器直接跑在沙盒 cgroup 下:"
        echo ""
        echo "    # 1. 创建沙盒"
        echo "    neu-sbox acquire --devices 6,7"
        echo "    # 2. 记下 sandbox_name（如 term_pengyt_12345.slice）"
        echo "    # 3. Docker 挂在沙盒 cgroup 下运行"
        echo "    docker run --rm --cgroup-parent sandbox_term_pengyt_12345.slice -it IMAGE bash"
        echo ""
        echo "  容器内所有进程自动归属沙盒 cgroup，无需 --device 参数。"
        echo "  --pid 将 bash PID 加入沙盒仅影响 bash 自身，不影响 Docker 容器。"
        ;;
esac
