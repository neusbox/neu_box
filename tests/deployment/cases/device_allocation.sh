#!/usr/bin/env bash

run_device_allocation_tests() {
    if ((SKIP_DEVICE)); then
        skip '按参数跳过设备分配与释放测试'
        return
    fi

    refresh_status
    local allocation_baseline
    allocation_baseline="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    if ((allocation_baseline == 0)); then
        skip '当前没有空闲设备，跳过设备分配与释放测试'
        return
    fi

    test_title '单卡任务分配与完成后释放'
    local marker task_id allocated_count running_idle
    marker="neu-box-device-$$_${RANDOM}"
    submit_task "printf '%s\\n' '$marker-start'; sleep 6; printf '%s\\n' '$marker-done'" 1
    task_id="$TASK_RESULT"
    wait_task_running "$task_id"
    allocated_count="$(json_array_length "$TASK_RESULT" devices)" \
        || fail '设备任务的 devices 字段无效'
    [[ "$allocated_count" == '1' ]] \
        || fail "设备任务应分配 1 张卡，实际为 $allocated_count"
    refresh_status
    running_idle="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    ((running_idle < allocation_baseline)) \
        || fail "任务运行时空闲卡未减少（基线 $allocation_baseline，当前 $running_idle）"
    wait_task_terminal "$task_id"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "设备任务未正常完成（状态: $TASK_STATE）"
    wait_idle_at_least "$allocation_baseline" 45
    pass '任务运行时独占 1 张卡，结束后空闲卡恢复'
}
