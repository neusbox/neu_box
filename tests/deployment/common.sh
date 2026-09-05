#!/usr/bin/env bash
# Worker 实机验收共享框架。由 tests/test_deployment.sh source。

deployment_init() {
    WORKER_URL="${WORKER_URL%/}"
    PYTHON_BIN="$(command -v python3 || true)"
    [[ -n "$PYTHON_BIN" ]] || { echo "缺少依赖: python3" >&2; exit 2; }
    command -v curl >/dev/null 2>&1 \
        || { echo "缺少依赖: curl" >&2; exit 2; }

    TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/neu-box-deployment.XXXXXX")"
    HTTP_BODY=''
    HTTP_STATUS=''
    TASK_STATE=''
    TASK_RESULT=''
    CREATED_TASK_IDS=()
    CLEANUP_PIDS=()
    CLEANUP_SANDBOXES=()
    PASSED=0
    SKIPPED=0
    TOTAL_DEVICES=0
    BASELINE_IDLE=0

    LOCAL_WORKER=0
    case "$WORKER_URL" in
        http://127.0.0.1:*|http://localhost:*|http://\[::1\]:*|https://127.0.0.1:*|https://localhost:*|https://\[::1\]:*)
            LOCAL_WORKER=1
            ;;
    esac

    trap deployment_cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

deployment_cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM

    local sandbox_name release_payload pid task_payload
    for sandbox_name in "${CLEANUP_SANDBOXES[@]}"; do
        [[ -n "$sandbox_name" ]] || continue
        release_payload="$(make_release_payload "$sandbox_name")"
        curl --noproxy '*' --silent --show-error --max-time 20 \
            --header 'Content-Type: application/json' \
            --data "$release_payload" \
            "$WORKER_URL/sandbox/release" >/dev/null 2>&1 || true
    done

    for pid in "${CLEANUP_PIDS[@]}"; do
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
        wait "$pid" 2>/dev/null || true
    done

    if ((${#CREATED_TASK_IDS[@]})); then
        task_payload="$(make_task_ids_payload "${CREATED_TASK_IDS[@]}")"
        curl --noproxy '*' --silent --show-error --max-time 20 \
            --request DELETE --header 'Content-Type: application/json' \
            --data "$task_payload" "$WORKER_URL/tasks" >/dev/null 2>&1 || true
    fi

    if [[ -n "${TEST_TMP:-}" && -d "$TEST_TMP" ]]; then
        rm -rf -- "$TEST_TMP"
    fi
    exit "$exit_code"
}

print_deployment_header() {
    printf '========================================\n'
    printf '       Neu Box Worker 实机验收\n'
    printf '========================================\n'
    printf 'Worker: %s\n' "$WORKER_URL"
    printf '用户:   %s\n' "$TEST_USER"
    printf '说明:   会创建真实任务，并可能短暂独占多张设备卡\n'
}

print_deployment_summary() {
    printf '\n========================================\n'
    printf '验收通过：%d 项通过，%d 项跳过\n' "$PASSED" "$SKIPPED"
    printf '设备基线：%s/%s 张空闲\n' "$BASELINE_IDLE" "$TOTAL_DEVICES"
    printf '========================================\n'
}

test_title() {
    printf '\n[TEST] %s\n' "$1"
}

pass() {
    PASSED=$((PASSED + 1))
    printf '[PASS] %s\n' "$1"
}

skip() {
    SKIPPED=$((SKIPPED + 1))
    printf '[SKIP] %s\n' "$1"
}

fail() {
    printf '[FAIL] %s\n' "$1" >&2
    if [[ -n "$HTTP_STATUS" ]]; then
        printf '       HTTP %s: %s\n' "$HTTP_STATUS" "${HTTP_BODY:0:1000}" >&2
    fi
    exit 1
}

json_value() {
    local document="$1"
    local path="$2"
    "$PYTHON_BIN" -c '
import json
import sys

value = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    if part:
        value = value[int(part)] if isinstance(value, list) else value[part]
if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
elif value is None:
    print("")
elif isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
' "$path" <<<"$document"
}

json_array_length() {
    local document="$1"
    local path="$2"
    "$PYTHON_BIN" -c '
import json
import sys

value = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    if part:
        value = value[int(part)] if isinstance(value, list) else value[part]
if not isinstance(value, list):
    raise SystemExit(f"{sys.argv[1]} 不是数组")
print(len(value))
' "$path" <<<"$document"
}

json_array_lines() {
    local document="$1"
    local path="$2"
    "$PYTHON_BIN" -c '
import json
import sys

value = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    if part:
        value = value[int(part)] if isinstance(value, list) else value[part]
if not isinstance(value, list):
    raise SystemExit(f"{sys.argv[1]} 不是数组")
for item in value:
    print(item)
' "$path" <<<"$document"
}

json_utf8_length() {
    "$PYTHON_BIN" -c 'import sys; print(len(sys.stdin.read().encode("utf-8")))'
}

json_string_utf8_length() {
    local document="$1"
    local path="$2"
    "$PYTHON_BIN" -c '
import json
import sys

value = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    if part:
        value = value[int(part)] if isinstance(value, list) else value[part]
if not isinstance(value, str):
    raise SystemExit(f"{sys.argv[1]} 不是字符串")
print(len(value.encode("utf-8")))
' "$path" <<<"$document"
}

make_task_payload() {
    local command="$1"
    local device_num="$2"
    local priority="$3"
    local cpu="$4"
    local memory="$5"
    local mem_unit="$6"
    shift 6
    "$PYTHON_BIN" -c '
import json
import sys

payload = {
    "user_id": sys.argv[1],
    "command": sys.argv[2],
    "device_num": int(sys.argv[3]),
    "priority": int(sys.argv[4]),
    "cpu": int(sys.argv[5]),
    "memory": int(sys.argv[6]),
    "mem_unit": sys.argv[7],
}
if len(sys.argv) > 8:
    payload["device_ids"] = sys.argv[8:]
print(json.dumps(payload))
' "$TEST_USER" "$command" "$device_num" "$priority" \
        "$cpu" "$memory" "$mem_unit" "$@"
}

make_acquire_payload() {
    local pid="$1"
    local device_num="${2:-1}"
    local cpu="${3:-0}"
    local memory="${4:-0}"
    local mem_unit="${5:-GB}"
    local -a device_ids=()
    if (($# > 5)); then
        device_ids=("${@:6}")
    fi
    "$PYTHON_BIN" -c '
import json
import sys

payload = {
    "username": sys.argv[1],
    "pid": int(sys.argv[2]),
    "device_num": int(sys.argv[3]),
    "cpu": int(sys.argv[4]),
    "memory": int(sys.argv[5]),
    "mem_unit": sys.argv[6],
}
if len(sys.argv) > 7:
    payload["device_ids"] = sys.argv[7:]
print(json.dumps(payload))
' "$TEST_USER" "$pid" "$device_num" "$cpu" "$memory" "$mem_unit" \
        "${device_ids[@]}"
}

make_task_ids_payload() {
    "$PYTHON_BIN" -c '
import json
import sys
print(json.dumps({"task_ids": sys.argv[1:]}))
' "$@"
}

make_release_payload() {
    "$PYTHON_BIN" -c '
import json
import sys
print(json.dumps({"sandbox_name": sys.argv[1]}))
' "$1"
}

make_join_payload() {
    "$PYTHON_BIN" -c '
import json
import sys
print(json.dumps({
    "username": sys.argv[1],
    "pid": int(sys.argv[2]),
    "sandbox_name": sys.argv[3],
}))
' "$TEST_USER" "$1" "$2"
}

idle_device_ids() {
    local document="$1"
    "$PYTHON_BIN" -c '
import json
import sys

status = json.load(sys.stdin).get("dev_status", {})
for device_id, busy in sorted(status.items(), key=lambda item: int(item[0])):
    if int(busy) == 0:
        print(int(device_id))
' <<<"$document"
}

device_minor() {
    printf '%s\n' "${1##*:}"
}

device_node_for_minor() {
    local minor="$1"
    "$PYTHON_BIN" -c '
import os
import re
import stat
import sys

minor = int(sys.argv[1])
pattern = re.compile(r"(?:nvidia|davinci)[0-9]+$")
for entry in sorted(os.scandir("/dev"), key=lambda item: item.name):
    try:
        mode = entry.stat(follow_symlinks=True).st_mode
        rdev = entry.stat(follow_symlinks=True).st_rdev
    except OSError:
        continue
    if pattern.fullmatch(entry.name) and stat.S_ISCHR(mode) and os.minor(rdev) == minor:
        print(entry.path)
        raise SystemExit(0)
raise SystemExit(1)
' "$minor"
}

can_open_device() {
    "$PYTHON_BIN" -c '
import os
import sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NONBLOCK)
os.close(fd)
' "$1" >/dev/null 2>&1
}

queue_task_value() {
    local document="$1"
    local task_id="$2"
    local field="$3"
    "$PYTHON_BIN" -c '
import json
import sys

document = json.load(sys.stdin)
for task in document.get("queue", []):
    if task.get("task_id") == sys.argv[1]:
        print(task[sys.argv[2]])
        break
else:
    raise SystemExit(1)
' "$task_id" "$field" <<<"$document"
}

sandbox_pid_state() {
    local document="$1"
    local sandbox_name="$2"
    local first_pid="$3"
    local second_pid="$4"
    "$PYTHON_BIN" -c '
import json
import sys

doc = json.load(sys.stdin)
for sandbox in doc.get("sandboxes", []):
    if sandbox.get("name") == sys.argv[1]:
        pids = {int(pid) for pid in sandbox.get("pids", [])}
        first = int(sys.argv[2])
        second = int(sys.argv[3])
        print(f"present:{int(first in pids)}:{int(second in pids)}")
        break
else:
    print("absent:0:0")
' "$sandbox_name" "$first_pid" "$second_pid" <<<"$document"
}

urlencode() {
    "$PYTHON_BIN" -c \
        'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' \
        "$1"
}

http_request() {
    local method="$1"
    local path="$2"
    local data="${3-}"
    local output="$TEST_TMP/http.body"
    local -a args=(
        curl --noproxy '*' --silent --show-error
        --connect-timeout 5 --max-time 30
        --output "$output" --write-out '%{http_code}'
        --request "$method"
    )
    rm -f -- "$output"
    if [[ -n "$data" ]]; then
        args+=(--header 'Content-Type: application/json' --data "$data")
    fi
    HTTP_STATUS="$("${args[@]}" "$WORKER_URL$path" || true)"
    HTTP_BODY=''
    if [[ -f "$output" ]]; then
        HTTP_BODY="$(<"$output")"
    fi
}

expect_http() {
    local expected="$1"
    local context="$2"
    if [[ "$HTTP_STATUS" != "$expected" ]]; then
        fail "$context：期望 HTTP $expected，实际为 ${HTTP_STATUS:-无响应}"
    fi
}

refresh_status() {
    http_request GET '/status'
    expect_http 200 '读取 Worker 状态失败'
}

submit_task_custom() {
    local command="$1"
    local device_num="$2"
    local priority="$3"
    local cpu="$4"
    local memory="$5"
    local mem_unit="$6"
    shift 6
    local payload
    payload="$(make_task_payload \
        "$command" "$device_num" "$priority" "$cpu" "$memory" "$mem_unit" "$@")"
    http_request POST '/tasks' "$payload"
    expect_http 202 '提交任务失败'
    TASK_RESULT="$(json_value "$HTTP_BODY" task_id)" \
        || fail '任务响应缺少 task_id'
    [[ -n "$TASK_RESULT" ]] || fail '任务响应中的 task_id 为空'
    CREATED_TASK_IDS+=("$TASK_RESULT")
}

submit_task() {
    local command="$1"
    local device_num="$2"
    local priority="${3:-0}"
    local device_id="${4:-}"
    if [[ -n "$device_id" ]]; then
        submit_task_custom "$command" 0 "$priority" 0 0 GB "$device_id"
    else
        submit_task_custom "$command" "$device_num" "$priority" 0 0 GB
    fi
}

wait_task_terminal() {
    local task_id="$1"
    local deadline=$((SECONDS + TASK_TIMEOUT))
    TASK_STATE=''
    TASK_RESULT=''
    while ((SECONDS < deadline)); do
        http_request GET "/tasks/$task_id"
        expect_http 200 "查询任务 $task_id 失败"
        TASK_STATE="$(json_value "$HTTP_BODY" status)" \
            || fail '任务响应缺少 status'
        if [[ "$TASK_STATE" == 'completed' || "$TASK_STATE" == 'failed' ]]; then
            TASK_RESULT="$HTTP_BODY"
            return 0
        fi
        sleep "$POLL_INTERVAL"
    done
    fail "任务 $task_id 在 ${TASK_TIMEOUT}s 内未结束（当前状态: ${TASK_STATE:-unknown}）"
}

wait_task_running() {
    local task_id="$1"
    local deadline=$((SECONDS + TASK_TIMEOUT))
    TASK_STATE=''
    TASK_RESULT=''
    while ((SECONDS < deadline)); do
        http_request GET "/tasks/$task_id"
        expect_http 200 "查询任务 $task_id 失败"
        TASK_STATE="$(json_value "$HTTP_BODY" status)" \
            || fail '任务响应缺少 status'
        if [[ "$TASK_STATE" == 'running' ]]; then
            TASK_RESULT="$HTTP_BODY"
            return 0
        fi
        if [[ "$TASK_STATE" == 'completed' || "$TASK_STATE" == 'failed' ]]; then
            fail "任务在观察到 running 前已结束（状态: $TASK_STATE）"
        fi
        sleep 1
    done
    fail "任务 $task_id 在 ${TASK_TIMEOUT}s 内未开始运行"
}

wait_log_contains() {
    local task_id="$1"
    local marker="$2"
    local timeout="${3:-20}"
    local deadline=$((SECONDS + timeout))
    while ((SECONDS < deadline)); do
        http_request GET "/tasks/$task_id/log?raw=1"
        expect_http 200 "读取任务 $task_id 日志失败"
        [[ "$HTTP_BODY" == *"$marker"* ]] && return 0
        sleep 1
    done
    fail "任务 $task_id 日志在 ${timeout}s 内未出现标记 $marker"
}

wait_idle_at_least() {
    local expected="$1"
    local timeout="$2"
    local deadline=$((SECONDS + timeout))
    local idle=0
    while ((SECONDS < deadline)); do
        refresh_status
        idle="$(json_value "$HTTP_BODY" idle_devices)" \
            || fail 'status 缺少 idle_devices'
        if [[ "$idle" =~ ^[0-9]+$ ]] && ((idle >= expected)); then
            return 0
        fi
        sleep "$POLL_INTERVAL"
    done
    fail "设备未在 ${timeout}s 内恢复到至少 $expected 张空闲卡（当前: $idle）"
}

wait_process_gone() {
    local pid="$1"
    local timeout="${2:-20}"
    local deadline=$((SECONDS + timeout))
    local state=''
    while ((SECONDS < deadline)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid" 2>/dev/null || true
            return 0
        fi
        if [[ -r "/proc/$pid/stat" ]]; then
            state="$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null || true)"
            if [[ "$state" == 'Z' ]]; then
                wait "$pid" 2>/dev/null || true
                return 0
            fi
        fi
        sleep 1
    done
    fail "进程 $pid 在 ${timeout}s 内未退出"
}

register_cleanup_pid() {
    CLEANUP_PIDS+=("$1")
}

register_cleanup_sandbox() {
    CLEANUP_SANDBOXES+=("$1")
}

start_detached_sleep() {
    # 显式 release 会用 cgroup.kill 结束测试进程。用短命父进程 fork 出孤儿，
    # 避免 Bash 在任意后续命令旁打印误导性的 "Killed sleep 300" 作业通知。
    "$PYTHON_BIN" -c '
import os
import time

pid = os.fork()
if pid:
    print(pid, flush=True)
    os._exit(0)

os.setsid()
devnull = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(devnull, fd)
if devnull > 2:
    os.close(devnull)
while True:
    time.sleep(300)
'
}

running_inside_neu_box_sandbox() {
    grep -q 'sandbox_' "/proc/$$/cgroup" 2>/dev/null
}
