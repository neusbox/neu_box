#!/usr/bin/env bash

run_task_cancel_delete_tests() {
    test_title '已完成任务及日志删除'
    local completed_task payload deleted
    submit_task 'printf "delete-me\\n"' 0
    completed_task="$TASK_RESULT"
    wait_task_terminal "$completed_task"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail '用于删除测试的任务未正常完成'
    payload="$(make_task_ids_payload "$completed_task")"
    http_request DELETE '/tasks' "$payload"
    expect_http 200 '删除已完成任务失败'
    deleted="$(json_value "$HTTP_BODY" deleted)" \
        || fail '删除响应缺少 deleted'
    [[ "$deleted" == '1' ]] || fail "删除计数错误: $deleted"
    http_request GET "/tasks/$completed_task"
    expect_http 404 '已删除任务仍可查询'
    http_request GET "/tasks/$completed_task/log?raw=1"
    expect_http 200 '查询已删除任务日志失败'
    [[ -z "$HTTP_BODY" ]] || fail '删除任务后日志文件仍有内容'
    pass '已完成任务记录与对应日志同时删除'

    if ((SKIP_DEVICE)); then
        skip '按参数跳过 queued 删除与 running 取消测试'
        return
    fi

    refresh_status
    local cancel_baseline
    cancel_baseline="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    if ((cancel_baseline == 0)); then
        skip '当前没有空闲设备，跳过 queued 删除与 running 取消测试'
        return
    fi

    local -a idle_ids
    mapfile -t idle_ids < <(idle_device_ids "$HTTP_BODY")
    local device_id blocker_task queued_task queued_marker queued_state cancel_error
    device_id="${idle_ids[0]}"

    test_title '排队任务删除且不会被执行'
    submit_task 'sleep 60' 0 0 "$device_id"
    blocker_task="$TASK_RESULT"
    wait_task_running "$blocker_task"

    queued_marker="SHOULD_NOT_RUN_$$_${RANDOM}"
    submit_task "printf '%s\\n' '$queued_marker'" 0 0 "$device_id"
    queued_task="$TASK_RESULT"
    http_request GET "/tasks/$queued_task"
    expect_http 200 '查询待删除的排队任务失败'
    queued_state="$(json_value "$HTTP_BODY" status)" \
        || fail '排队任务缺少 status'
    [[ "$queued_state" == 'queued' ]] \
        || fail "同卡任务没有保持 queued（状态: $queued_state）"

    payload="$(make_task_ids_payload "$queued_task")"
    http_request DELETE '/tasks' "$payload"
    expect_http 200 '删除排队任务失败'
    deleted="$(json_value "$HTTP_BODY" deleted)" \
        || fail '删除排队任务响应缺少 deleted'
    [[ "$deleted" == '1' ]] || fail "排队任务删除计数错误: $deleted"
    http_request GET "/tasks/$queued_task"
    expect_http 404 '排队任务删除后仍可查询'
    http_request GET "/tasks/$queued_task/log?raw=1"
    expect_http 200 '查询已删除排队任务日志失败'
    [[ "$HTTP_BODY" != *"$queued_marker"* ]] \
        || fail '已删除的排队任务仍被执行'
    pass 'queued 任务立即删除，命令未执行且无残留日志'

    test_title '运行中任务异步取消并释放设备'
    payload="$(make_task_ids_payload "$blocker_task")"
    http_request DELETE '/tasks' "$payload"
    expect_http 200 '取消运行中任务失败'
    deleted="$(json_value "$HTTP_BODY" deleted)" \
        || fail '取消响应缺少 deleted'
    [[ "$deleted" == '1' ]] || fail "运行中任务取消计数错误: $deleted"
    wait_task_terminal "$blocker_task"
    [[ "$TASK_STATE" == 'failed' ]] \
        || fail "取消后的任务状态应为 failed，实际为 $TASK_STATE"
    cancel_error="$(json_value "$TASK_RESULT" result.error)" \
        || fail '取消后的任务缺少 result.error'
    [[ "$cancel_error" == *'手动取消'* ]] \
        || fail "取消原因不明确: ${cancel_error:-missing}"
    wait_idle_at_least "$cancel_baseline" 45
    pass "running 任务异步取消，结果保留为 failed，设备 $device_id 已释放"
}
