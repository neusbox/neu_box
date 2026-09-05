#!/usr/bin/env bash

run_core_tests() {
    test_title '健康检查与 API 版本'
    http_request GET '/healthz'
    expect_http 200 'Worker 健康检查失败'
    local role api_version worker_version
    role="$(json_value "$HTTP_BODY" role)" || fail 'healthz 缺少 role'
    api_version="$(json_value "$HTTP_BODY" api_version)" \
        || fail 'healthz 缺少 api_version'
    worker_version="$(json_value "$HTTP_BODY" version)" \
        || fail 'healthz 缺少 version'
    [[ "$role" == 'worker' ]] \
        || fail "healthz role 应为 worker，实际为 $role"
    [[ "$api_version" =~ ^[0-9]+$ ]] \
        || fail "api_version 不是整数: $api_version"
    ((api_version >= 2)) \
        || fail "Worker API 版本过旧: $api_version（要求 >= 2）"
    pass "Worker $worker_version 在线，api_version=$api_version"

    test_title '资源状态与设备基线'
    refresh_status
    local active_sandboxes
    TOTAL_DEVICES="$(json_value "$HTTP_BODY" total_devices)" \
        || fail 'status 缺少 total_devices'
    BASELINE_IDLE="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    active_sandboxes="$(json_value "$HTTP_BODY" active_sandboxes)" \
        || fail 'status 缺少 active_sandboxes'
    [[ "$TOTAL_DEVICES" =~ ^[0-9]+$ ]] \
        || fail "total_devices 不是整数: $TOTAL_DEVICES"
    [[ "$BASELINE_IDLE" =~ ^[0-9]+$ ]] \
        || fail "idle_devices 不是整数: $BASELINE_IDLE"
    ((BASELINE_IDLE <= TOTAL_DEVICES)) \
        || fail 'idle_devices 大于 total_devices'
    pass "资源状态正常：空闲设备 $BASELINE_IDLE/$TOTAL_DEVICES，活跃沙盒 $active_sandboxes"

    test_title '旧版命令路由已关闭'
    http_request GET '/command/queue'
    expect_http 404 '旧路由 /command/queue 仍然可访问'
    pass '旧版 /command/queue 返回 404'

    test_title '不存在的系统用户会被拒绝'
    local missing_user missing_payload missing_error
    missing_user="neu_box_missing_$$_${RANDOM}"
    missing_payload="$("$PYTHON_BIN" -c '
import json
import sys
print(json.dumps({"user_id": sys.argv[1], "command": "true", "device_num": 0}))
' "$missing_user")"
    http_request POST '/tasks' "$missing_payload"
    expect_http 400 '不存在的用户未被拒绝'
    missing_error="$(json_value "$HTTP_BODY" error)" \
        || fail '不存在用户的响应缺少 error 字段'
    if [[ "$missing_error" != *"$missing_user"* ]] \
        || [[ "$missing_error" != *'不存在'* \
            && "$missing_error" != *'Unknown user'* ]]; then
        fail '不存在用户的错误响应没有明确说明用户不存在'
    fi
    pass '不存在的用户返回 HTTP 400 和明确错误信息'

    test_title '零设备正常任务与日志'
    local normal_marker normal_task normal_rc
    normal_marker="neu-box-smoke-$$_${RANDOM}"
    submit_task "printf '%s\\n' '$normal_marker'" 0
    normal_task="$TASK_RESULT"
    wait_task_terminal "$normal_task"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "正常任务状态应为 completed，实际为 $TASK_STATE"
    normal_rc="$(json_value "$TASK_RESULT" result.returncode)" \
        || fail '正常任务结果缺少 result.returncode'
    [[ "$normal_rc" == '0' ]] \
        || fail "正常任务返回码应为 0，实际为 $normal_rc"
    http_request GET "/tasks/$normal_task/log?raw=1"
    expect_http 200 '读取正常任务日志失败'
    [[ "$HTTP_BODY" == *"$normal_marker"* ]] \
        || fail '正常任务日志缺少预期标记'
    pass '正常任务完成，返回码与日志正确'

    test_title 'Shell 解析错误会进入任务日志'
    local error_task error_rc
    submit_task 'if' 0
    error_task="$TASK_RESULT"
    wait_task_terminal "$error_task"
    [[ "$TASK_STATE" == 'failed' ]] \
        || fail "语法错误任务状态应为 failed，实际为 $TASK_STATE"
    error_rc="$(json_value "$TASK_RESULT" result.returncode)" \
        || fail '错误任务结果缺少 result.returncode'
    [[ "$error_rc" =~ ^-?[0-9]+$ ]] \
        || fail "错误任务返回码不是整数: $error_rc"
    ((error_rc != 0)) || fail '语法错误任务意外返回 0'
    http_request GET "/tasks/$error_task/log?raw=1"
    expect_http 200 '读取错误任务日志失败'
    if ! grep -Eiq 'syntax error|unexpected end|语法错误' <<<"$HTTP_BODY"; then
        fail '任务日志没有返回 Shell 解析错误信息'
    fi
    pass 'Shell 解析错误、非零返回码和失败状态均正确返回'
}
