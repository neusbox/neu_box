#!/usr/bin/env bash

run_priority_tests() {
    test_title '优先级参数边界'
    local invalid_priority payload priority_error
    for invalid_priority in -1 2; do
        payload="$(make_task_payload 'true' 0 "$invalid_priority" 0 0 GB)"
        http_request POST '/tasks' "$payload"
        expect_http 400 "非法 priority=$invalid_priority 未被拒绝"
        priority_error="$(json_value "$HTTP_BODY" error)" \
            || fail "priority=$invalid_priority 的响应缺少 error 字段"
        [[ "$priority_error" == *priority* ]] \
            || fail "priority=$invalid_priority 的错误信息不明确"
    done
    pass 'priority 仅接受 0（普通）和 1（赶论文）'

    if ((SKIP_DEVICE)); then
        skip '按参数跳过优先级设备队列排序测试'
        return
    fi

    refresh_status
    local priority_baseline
    priority_baseline="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    if ((priority_baseline == 0)); then
        skip '当前没有空闲设备，跳过优先级设备队列排序测试'
        return
    fi

    test_title '高优先级插队与同级 FIFO'
    local -a idle_ids
    mapfile -t idle_ids < <(idle_device_ids "$HTTP_BODY")
    local device_id blocker normal high_one high_two
    local high_one_position high_two_position normal_position
    local high_one_priority high_two_priority normal_priority
    local high_one_started high_two_started normal_started count
    device_id="${idle_ids[0]}"

    # blocker 固定占卡，使后续三条同卡任务稳定保持 queued。
    submit_task 'sleep 8' 0 0 "$device_id"
    blocker="$TASK_RESULT"
    wait_task_running "$blocker"
    count="$(json_array_length "$TASK_RESULT" devices)" \
        || fail '优先级 blocker 的 devices 字段无效'
    [[ "$count" == '1' ]] \
        || fail "优先级 blocker 应占用 1 张卡，实际为 $count"

    submit_task 'sleep 1' 0 0 "$device_id"
    normal="$TASK_RESULT"
    submit_task 'sleep 1' 0 1 "$device_id"
    high_one="$TASK_RESULT"
    submit_task 'sleep 1' 0 1 "$device_id"
    high_two="$TASK_RESULT"

    http_request GET '/tasks'
    expect_http 200 '读取优先级测试队列失败'
    high_one_position="$(queue_task_value "$HTTP_BODY" "$high_one" position)" \
        || fail '队列中缺少第一个高优先级任务'
    high_two_position="$(queue_task_value "$HTTP_BODY" "$high_two" position)" \
        || fail '队列中缺少第二个高优先级任务'
    normal_position="$(queue_task_value "$HTTP_BODY" "$normal" position)" \
        || fail '队列中缺少普通优先级任务'
    high_one_priority="$(queue_task_value "$HTTP_BODY" "$high_one" priority)" \
        || fail '第一个高优先级任务缺少 priority'
    high_two_priority="$(queue_task_value "$HTTP_BODY" "$high_two" priority)" \
        || fail '第二个高优先级任务缺少 priority'
    normal_priority="$(queue_task_value "$HTTP_BODY" "$normal" priority)" \
        || fail '普通优先级任务缺少 priority'
    [[ "$high_one_priority" == '1' \
        && "$high_two_priority" == '1' \
        && "$normal_priority" == '0' ]] \
        || fail '队列没有保留任务 priority'
    ((high_one_position < high_two_position \
        && high_two_position < normal_position)) \
        || fail "优先级排位错误：高1=$high_one_position，高2=$high_two_position，普通=$normal_position"

    wait_task_terminal "$blocker"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "优先级 blocker 未正常完成（状态: $TASK_STATE）"
    wait_task_terminal "$high_one"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "第一个高优先级任务未正常完成（状态: $TASK_STATE）"
    high_one_started="$(json_value "$TASK_RESULT" started_at)" \
        || fail '第一个高优先级任务缺少 started_at'
    wait_task_terminal "$high_two"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "第二个高优先级任务未正常完成（状态: $TASK_STATE）"
    high_two_started="$(json_value "$TASK_RESULT" started_at)" \
        || fail '第二个高优先级任务缺少 started_at'
    wait_task_terminal "$normal"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "普通优先级任务未正常完成（状态: $TASK_STATE）"
    normal_started="$(json_value "$TASK_RESULT" started_at)" \
        || fail '普通优先级任务缺少 started_at'

    "$PYTHON_BIN" -c '
import sys
high_one, high_two, normal = map(float, sys.argv[1:])
if not high_one < high_two < normal:
    raise SystemExit(1)
' "$high_one_started" "$high_two_started" "$normal_started" \
        || fail "实际启动顺序错误：高1=$high_one_started，高2=$high_two_started，普通=$normal_started"
    wait_idle_at_least "$priority_baseline" 45
    pass "priority=1 先于 0，同为 1 时保持 FIFO（设备 $device_id）"
}
