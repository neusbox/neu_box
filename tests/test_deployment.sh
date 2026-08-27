#!/usr/bin/env bash
# Neu Box Worker 实机一键验收。
#
# 默认在 Worker 本机测试完整链路。脚本会提交真实任务、短暂占用一张空闲卡，
# 并在本机验证 Reaper 对“父进程退出、子进程仍存活”的处理。

set -Eeuo pipefail

WORKER_URL="${NEU_BOX_WORKER_URL:-http://127.0.0.1:59075}"
TEST_USER="${NEU_BOX_TEST_USER:-$(id -un)}"
TASK_TIMEOUT=120
REAPER_TIMEOUT=100
SKIP_DEVICE=0
SKIP_REAPER=0
POLL_INTERVAL=2

usage() {
    cat <<'EOF'
用法: tests/test_deployment.sh [选项]

选项:
  --url URL                 Worker 地址（默认 http://127.0.0.1:59075）
  --user USER               执行任务的系统用户（默认当前用户）
  --timeout SECONDS         单个任务的最长等待时间（默认 120）
  --reaper-timeout SECONDS  Reaper 回收的最长等待时间（默认 100）
  --skip-device             跳过所有真实设备占用测试
  --skip-reaper             跳过父子进程与设备最终回收测试
  -h, --help                显示帮助

环境变量:
  NEU_BOX_WORKER_URL        与 --url 相同
  NEU_BOX_TEST_USER         与 --user 相同

示例:
  sudo -u pengyt ./tests/test_deployment.sh
  ./tests/test_deployment.sh --url http://10.0.0.8:59075 --user pengyt
  ./tests/test_deployment.sh --skip-device
EOF
}

while (($#)); do
    case "$1" in
        --url)
            (($# >= 2)) || { echo "--url 缺少参数" >&2; exit 2; }
            WORKER_URL="$2"
            shift 2
            ;;
        --user)
            (($# >= 2)) || { echo "--user 缺少参数" >&2; exit 2; }
            TEST_USER="$2"
            shift 2
            ;;
        --timeout)
            (($# >= 2)) || { echo "--timeout 缺少参数" >&2; exit 2; }
            TASK_TIMEOUT="$2"
            shift 2
            ;;
        --reaper-timeout)
            (($# >= 2)) || { echo "--reaper-timeout 缺少参数" >&2; exit 2; }
            REAPER_TIMEOUT="$2"
            shift 2
            ;;
        --skip-device)
            SKIP_DEVICE=1
            shift
            ;;
        --skip-reaper)
            SKIP_REAPER=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "$TASK_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "--timeout 必须是正整数" >&2
    exit 2
fi
if [[ ! "$REAPER_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    echo "--reaper-timeout 必须是正整数" >&2
    exit 2
fi
if [[ -z "$TEST_USER" ]]; then
    echo "测试用户不能为空" >&2
    exit 2
fi

WORKER_URL="${WORKER_URL%/}"
PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    echo "缺少依赖: python3" >&2
    exit 2
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "缺少依赖: curl" >&2
    exit 2
fi

TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/neu-box-deployment.XXXXXX")"
HTTP_BODY=''
HTTP_STATUS=''
TASK_STATE=''
TASK_RESULT=''
CREATED_TASK_IDS=()
REAPER_PARENT=''
REAPER_CHILD=''
REAPER_SANDBOX=''
PASSED=0
SKIPPED=0

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

make_task_payload() {
    local command="$1"
    local device_num="$2"
    "$PYTHON_BIN" -c '
import json
import sys

print(json.dumps({
    "user_id": sys.argv[1],
    "command": sys.argv[2],
    "cpu": 0,
    "memory": 0,
    "mem_unit": "GB",
    "device_num": int(sys.argv[3]),
}))
' "$TEST_USER" "$command" "$device_num"
}

make_acquire_payload() {
    local pid="$1"
    "$PYTHON_BIN" -c '
import json
import sys

print(json.dumps({
    "username": sys.argv[1],
    "pid": int(sys.argv[2]),
    "device_num": 1,
    "cpu": 0,
    "memory": 0,
    "mem_unit": "GB",
}))
' "$TEST_USER" "$pid"
}

make_task_ids_payload() {
    "$PYTHON_BIN" -c '
import json
import sys
print(json.dumps({"task_ids": sys.argv[1:]}))
' "$@"
}

urlencode() {
    "$PYTHON_BIN" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
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

cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM

    if [[ -n "$REAPER_CHILD" ]] && kill -0 "$REAPER_CHILD" 2>/dev/null; then
        kill "$REAPER_CHILD" 2>/dev/null || true
    fi
    if [[ -n "$REAPER_PARENT" ]] && kill -0 "$REAPER_PARENT" 2>/dev/null; then
        kill "$REAPER_PARENT" 2>/dev/null || true
        wait "$REAPER_PARENT" 2>/dev/null || true
    fi
    if [[ -n "$REAPER_SANDBOX" ]]; then
        curl --noproxy '*' --silent --show-error --max-time 20 \
            --header 'Content-Type: application/json' \
            --data "{\"sandbox_name\":\"$REAPER_SANDBOX\"}" \
            "$WORKER_URL/sandbox/release" >/dev/null 2>&1 || true
    fi
    if ((${#CREATED_TASK_IDS[@]})); then
        local task_payload
        task_payload="$(make_task_ids_payload "${CREATED_TASK_IDS[@]}")"
        curl --noproxy '*' --silent --show-error --max-time 20 \
            --request DELETE --header 'Content-Type: application/json' \
            --data "$task_payload" "$WORKER_URL/tasks" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$TEST_TMP"
    exit "$exit_code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

submit_task() {
    local command="$1"
    local device_num="$2"
    local payload
    payload="$(make_task_payload "$command" "$device_num")"
    http_request POST '/tasks' "$payload"
    expect_http 202 '提交任务失败'
    TASK_RESULT="$(json_value "$HTTP_BODY" task_id)" || fail '任务响应缺少 task_id'
    [[ -n "$TASK_RESULT" ]] || fail '任务响应中的 task_id 为空'
    CREATED_TASK_IDS+=("$TASK_RESULT")
}

wait_task_terminal() {
    local task_id="$1"
    local deadline=$((SECONDS + TASK_TIMEOUT))
    TASK_STATE=''
    TASK_RESULT=''
    while ((SECONDS < deadline)); do
        http_request GET "/tasks/$task_id"
        expect_http 200 "查询任务 $task_id 失败"
        TASK_STATE="$(json_value "$HTTP_BODY" status)" || fail '任务响应缺少 status'
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
        TASK_STATE="$(json_value "$HTTP_BODY" status)" || fail '任务响应缺少 status'
        if [[ "$TASK_STATE" == 'running' ]]; then
            TASK_RESULT="$HTTP_BODY"
            return 0
        fi
        if [[ "$TASK_STATE" == 'completed' || "$TASK_STATE" == 'failed' ]]; then
            fail "设备任务在观察到 running 前已结束（状态: $TASK_STATE）"
        fi
        sleep 1
    done
    fail "设备任务在 ${TASK_TIMEOUT}s 内未开始运行"
}

wait_idle_at_least() {
    local expected="$1"
    local timeout="$2"
    local deadline=$((SECONDS + timeout))
    local idle=0
    while ((SECONDS < deadline)); do
        refresh_status
        idle="$(json_value "$HTTP_BODY" idle_devices)" || fail 'status 缺少 idle_devices'
        if [[ "$idle" =~ ^[0-9]+$ ]] && ((idle >= expected)); then
            return 0
        fi
        sleep "$POLL_INTERVAL"
    done
    fail "设备未在 ${timeout}s 内恢复到至少 $expected 张空闲卡（当前: $idle）"
}

sandbox_pid_state() {
    local document="$1"
    local sandbox_name="$2"
    local parent_pid="$3"
    local child_pid="$4"
    "$PYTHON_BIN" -c '
import json
import sys

doc = json.load(sys.stdin)
for sandbox in doc.get("sandboxes", []):
    if sandbox.get("name") == sys.argv[1]:
        pids = {int(pid) for pid in sandbox.get("pids", [])}
        parent = int(sys.argv[2])
        child = int(sys.argv[3])
        print(f"present:{int(parent in pids)}:{int(child in pids)}")
        break
else:
    print("absent:0:0")
' "$sandbox_name" "$parent_pid" "$child_pid" <<<"$document"
}

printf '========================================\n'
printf '       Neu Box Worker 实机验收\n'
printf '========================================\n'
printf 'Worker: %s\n' "$WORKER_URL"
printf '用户:   %s\n' "$TEST_USER"
printf '说明:   会创建真实任务，并可能短暂独占一张设备卡\n'

test_title '健康检查与 API 版本'
http_request GET '/healthz'
expect_http 200 'Worker 健康检查失败'
ROLE="$(json_value "$HTTP_BODY" role)" || fail 'healthz 缺少 role'
API_VERSION="$(json_value "$HTTP_BODY" api_version)" || fail 'healthz 缺少 api_version'
WORKER_VERSION="$(json_value "$HTTP_BODY" version)" || fail 'healthz 缺少 version'
[[ "$ROLE" == 'worker' ]] || fail "healthz role 应为 worker，实际为 $ROLE"
[[ "$API_VERSION" =~ ^[0-9]+$ ]] || fail "api_version 不是整数: $API_VERSION"
((API_VERSION >= 2)) || fail "Worker API 版本过旧: $API_VERSION（要求 >= 2）"
pass "Worker $WORKER_VERSION 在线，api_version=$API_VERSION"

test_title '资源状态与设备基线'
refresh_status
TOTAL_DEVICES="$(json_value "$HTTP_BODY" total_devices)" || fail 'status 缺少 total_devices'
BASELINE_IDLE="$(json_value "$HTTP_BODY" idle_devices)" || fail 'status 缺少 idle_devices'
ACTIVE_SANDBOXES="$(json_value "$HTTP_BODY" active_sandboxes)" || fail 'status 缺少 active_sandboxes'
[[ "$TOTAL_DEVICES" =~ ^[0-9]+$ ]] || fail "total_devices 不是整数: $TOTAL_DEVICES"
[[ "$BASELINE_IDLE" =~ ^[0-9]+$ ]] || fail "idle_devices 不是整数: $BASELINE_IDLE"
((BASELINE_IDLE <= TOTAL_DEVICES)) || fail 'idle_devices 大于 total_devices'
pass "资源状态正常：空闲设备 $BASELINE_IDLE/$TOTAL_DEVICES，活跃沙盒 $ACTIVE_SANDBOXES"

test_title '旧版命令路由已关闭'
http_request GET '/command/queue'
expect_http 404 '旧路由 /command/queue 仍然可访问'
pass '旧版 /command/queue 返回 404'

test_title '不存在的系统用户会被拒绝'
MISSING_USER="neu_box_missing_$$_${RANDOM}"
MISSING_PAYLOAD="$($PYTHON_BIN -c '
import json
import sys
print(json.dumps({"user_id": sys.argv[1], "command": "true", "device_num": 0}))
' "$MISSING_USER")"
http_request POST '/tasks' "$MISSING_PAYLOAD"
expect_http 400 '不存在的用户未被拒绝'
if [[ "$HTTP_BODY" != *'不存在'* && "$HTTP_BODY" != *'Unknown user'* ]]; then
    fail '不存在用户的错误响应没有明确说明用户不存在'
fi
pass '不存在的用户返回 HTTP 400 和明确错误信息'

test_title '零设备正常任务与日志'
NORMAL_MARKER="neu-box-smoke-$$_${RANDOM}"
submit_task "printf '%s\\n' '$NORMAL_MARKER'" 0
NORMAL_TASK="$TASK_RESULT"
wait_task_terminal "$NORMAL_TASK"
[[ "$TASK_STATE" == 'completed' ]] || fail "正常任务状态应为 completed，实际为 $TASK_STATE"
NORMAL_RC="$(json_value "$TASK_RESULT" returncode)" || fail '正常任务结果缺少 returncode'
[[ "$NORMAL_RC" == '0' ]] || fail "正常任务返回码应为 0，实际为 $NORMAL_RC"
http_request GET "/tasks/$NORMAL_TASK/log?raw=1"
expect_http 200 '读取正常任务日志失败'
[[ "$HTTP_BODY" == *"$NORMAL_MARKER"* ]] || fail '正常任务日志缺少预期标记'
pass '正常任务完成，返回码与日志正确'

test_title 'Shell 解析错误会进入任务日志'
submit_task 'if' 0
ERROR_TASK="$TASK_RESULT"
wait_task_terminal "$ERROR_TASK"
[[ "$TASK_STATE" == 'failed' ]] || fail "语法错误任务状态应为 failed，实际为 $TASK_STATE"
ERROR_RC="$(json_value "$TASK_RESULT" returncode)" || fail '错误任务结果缺少 returncode'
[[ "$ERROR_RC" =~ ^-?[0-9]+$ ]] || fail "错误任务返回码不是整数: $ERROR_RC"
((ERROR_RC != 0)) || fail '语法错误任务意外返回 0'
http_request GET "/tasks/$ERROR_TASK/log?raw=1"
expect_http 200 '读取错误任务日志失败'
if ! grep -Eiq 'syntax error|unexpected end|语法错误' <<<"$HTTP_BODY"; then
    fail '任务日志没有返回 Shell 解析错误信息'
fi
pass 'Shell 解析错误、非零返回码和失败状态均正确返回'

if ((SKIP_DEVICE)); then
    skip '按参数跳过设备分配与释放测试'
elif ((BASELINE_IDLE == 0)); then
    skip '当前没有空闲设备，跳过设备分配与释放测试'
else
    test_title '单卡任务分配与完成后释放'
    DEVICE_MARKER="neu-box-device-$$_${RANDOM}"
    submit_task "printf '%s\\n' '$DEVICE_MARKER-start'; sleep 12; printf '%s\\n' '$DEVICE_MARKER-done'" 1
    DEVICE_TASK="$TASK_RESULT"
    wait_task_running "$DEVICE_TASK"
    ALLOCATED_COUNT="$(json_array_length "$TASK_RESULT" devices)" || fail '设备任务的 devices 字段无效'
    [[ "$ALLOCATED_COUNT" == '1' ]] || fail "设备任务应分配 1 张卡，实际为 $ALLOCATED_COUNT"
    refresh_status
    RUNNING_IDLE="$(json_value "$HTTP_BODY" idle_devices)" || fail 'status 缺少 idle_devices'
    ((RUNNING_IDLE < BASELINE_IDLE)) || fail "任务运行时空闲卡未减少（基线 $BASELINE_IDLE，当前 $RUNNING_IDLE）"
    wait_task_terminal "$DEVICE_TASK"
    [[ "$TASK_STATE" == 'completed' ]] || fail "设备任务未正常完成（状态: $TASK_STATE）"
    wait_idle_at_least "$BASELINE_IDLE" 45
    pass '任务运行时独占 1 张卡，结束后空闲卡恢复'
fi

LOCAL_WORKER=0
case "$WORKER_URL" in
    http://127.0.0.1:*|http://localhost:*|http://\[::1\]:*|https://127.0.0.1:*|https://localhost:*|https://\[::1\]:*)
        LOCAL_WORKER=1
        ;;
esac

if ((SKIP_DEVICE)); then
    skip '设备测试已关闭，跳过 Reaper 实测'
elif ((SKIP_REAPER)); then
    skip '按参数跳过 Reaper 实测'
elif ((LOCAL_WORKER == 0)); then
    skip 'Worker 不是本机地址，无法用本机 PID 验证 Reaper'
elif [[ "$TEST_USER" != "$(id -un)" ]]; then
    skip '测试用户不是当前用户，无法安全创建归属匹配的本机测试进程'
elif grep -q 'sandbox_' "/proc/$$/cgroup" 2>/dev/null; then
    skip '当前测试脚本已处于 Neu Box sandbox 中，跳过会改变父 cgroup 的 Reaper 实测'
else
    refresh_status
    REAPER_BASELINE="$(json_value "$HTTP_BODY" idle_devices)" || fail 'status 缺少 idle_devices'
    if ((REAPER_BASELINE == 0)); then
        skip '当前没有空闲设备，跳过 Reaper 实测'
    else
        test_title 'Reaper 保留活跃子进程并最终回收设备'
        REAPER_GO_FILE="$TEST_TMP/reaper.go"
        REAPER_CHILD_FILE="$TEST_TMP/reaper.child"
        "$PYTHON_BIN" -c '
import os
import pathlib
import sys
import time

go_file = pathlib.Path(sys.argv[1])
child_file = pathlib.Path(sys.argv[2])
while not go_file.exists():
    time.sleep(0.05)
pid = os.fork()
if pid == 0:
    while True:
        time.sleep(60)
child_file.write_text(str(pid))
while True:
    time.sleep(60)
' "$REAPER_GO_FILE" "$REAPER_CHILD_FILE" &
        REAPER_PARENT=$!

        ACQUIRE_PAYLOAD="$(make_acquire_payload "$REAPER_PARENT")"
        http_request POST '/sandbox/acquire' "$ACQUIRE_PAYLOAD"
        expect_http 201 '为 Reaper 测试申请设备失败'
        REAPER_SANDBOX="$(json_value "$HTTP_BODY" sandbox_name)" || fail 'acquire 响应缺少 sandbox_name'
        REAPER_DEVICE_COUNT="$(json_array_length "$HTTP_BODY" devices)" || fail 'acquire 响应缺少 devices'
        [[ "$REAPER_DEVICE_COUNT" == '1' ]] || fail "Reaper 测试应分配 1 张卡，实际为 $REAPER_DEVICE_COUNT"
        touch "$REAPER_GO_FILE"

        CHILD_DEADLINE=$((SECONDS + 15))
        while [[ ! -s "$REAPER_CHILD_FILE" ]] && ((SECONDS < CHILD_DEADLINE)); do
            sleep 1
        done
        [[ -s "$REAPER_CHILD_FILE" ]] || fail '测试父进程没有按时创建子进程'
        REAPER_CHILD="$(<"$REAPER_CHILD_FILE")"
        [[ "$REAPER_CHILD" =~ ^[1-9][0-9]*$ ]] || fail "无效的测试子进程 PID: $REAPER_CHILD"
        kill -0 "$REAPER_CHILD" 2>/dev/null || fail '测试子进程未存活'

        DEAD_PARENT="$REAPER_PARENT"
        kill "$DEAD_PARENT"
        wait "$DEAD_PARENT" 2>/dev/null || true
        REAPER_PARENT=''

        REAPER_DEADLINE=$((SECONDS + REAPER_TIMEOUT))
        REAPER_STATE=''
        while ((SECONDS < REAPER_DEADLINE)); do
            http_request GET "/sandbox/list?username=$(urlencode "$TEST_USER")"
            expect_http 200 '查询 Reaper 测试沙盒失败'
            REAPER_STATE="$(sandbox_pid_state "$HTTP_BODY" "$REAPER_SANDBOX" "$DEAD_PARENT" "$REAPER_CHILD")" || fail '无法解析沙盒 PID 状态'
            if [[ "$REAPER_STATE" == 'present:0:1' ]]; then
                break
            fi
            sleep "$POLL_INTERVAL"
        done
        [[ "$REAPER_STATE" == 'present:0:1' ]] || fail "Reaper 未同步到存活子进程（状态: ${REAPER_STATE:-unknown}）"
        kill -0 "$REAPER_CHILD" 2>/dev/null || fail '父进程退出后，活跃子进程被错误清理'
        pass '父进程退出后，Reaper 识别并保留仍存活的子进程'

        kill "$REAPER_CHILD"
        REAPER_CHILD=''
        REAPER_DEADLINE=$((SECONDS + REAPER_TIMEOUT))
        while ((SECONDS < REAPER_DEADLINE)); do
            http_request GET "/sandbox/list?username=$(urlencode "$TEST_USER")"
            expect_http 200 '查询 Reaper 最终回收状态失败'
            REAPER_STATE="$(sandbox_pid_state "$HTTP_BODY" "$REAPER_SANDBOX" "$DEAD_PARENT" 0)" || fail '无法解析沙盒最终状态'
            if [[ "$REAPER_STATE" == 'absent:0:0' ]]; then
                break
            fi
            sleep "$POLL_INTERVAL"
        done
        [[ "$REAPER_STATE" == 'absent:0:0' ]] || fail "所有进程退出后沙盒仍未回收（状态: ${REAPER_STATE:-unknown}）"
        REAPER_SANDBOX=''
        wait_idle_at_least "$REAPER_BASELINE" "$REAPER_TIMEOUT"
        pass '最后一个子进程退出后，沙盒和设备均被 Reaper 回收'
    fi
fi

printf '\n========================================\n'
printf '验收通过：%d 项通过，%d 项跳过\n' "$PASSED" "$SKIPPED"
printf '设备基线：%s/%s 张空闲\n' "$BASELINE_IDLE" "$TOTAL_DEVICES"
printf '========================================\n'
