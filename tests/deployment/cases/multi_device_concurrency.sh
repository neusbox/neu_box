#!/usr/bin/env bash

run_multi_device_concurrency_tests() {
    if ((SKIP_DEVICE)); then
        skip '按参数跳过多卡与并发测试'
        return
    fi

    refresh_status
    local concurrency_baseline
    concurrency_baseline="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    if ((concurrency_baseline < 2)); then
        skip '多卡与并发测试需要至少 2 张空闲卡'
        return
    fi

    local -a idle_ids first_devices second_devices multi_devices
    mapfile -t idle_ids < <(idle_device_ids "$HTTP_BODY")
    local first_id second_id first_task second_task running_idle
    local first_finished second_started first_minor second_minor
    first_id="${idle_ids[0]}"
    second_id="${idle_ids[1]}"

    test_title '两张不同卡上的任务并发运行'
    submit_task 'sleep 10' 0 0 "$first_id"
    first_task="$TASK_RESULT"
    submit_task 'sleep 10' 0 0 "$second_id"
    second_task="$TASK_RESULT"
    wait_task_running "$first_task"
    mapfile -t first_devices < <(json_array_lines "$TASK_RESULT" devices)
    wait_task_running "$second_task"
    mapfile -t second_devices < <(json_array_lines "$TASK_RESULT" devices)
    [[ "${#first_devices[@]}" == '1' && "${#second_devices[@]}" == '1' ]] \
        || fail '两个并发任务没有各自分配一张卡'
    first_minor="$(device_minor "${first_devices[0]}")"
    second_minor="$(device_minor "${second_devices[0]}")"
    [[ "$first_minor" == "$first_id" && "$second_minor" == "$second_id" ]] \
        || fail "并发任务卡号错配：期望 $first_id/$second_id，实际 $first_minor/$second_minor"
    [[ "$first_minor" != "$second_minor" ]] \
        || fail '两个并发任务被分配到了同一张卡'
    refresh_status
    running_idle="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    ((running_idle <= concurrency_baseline - 2)) \
        || fail "两个任务运行时没有同时占用两张卡（基线 $concurrency_baseline，当前 $running_idle）"

    wait_task_terminal "$first_task"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "第一条并发任务未完成（状态: $TASK_STATE）"
    first_finished="$(json_value "$TASK_RESULT" finished_at)" \
        || fail '第一条并发任务缺少 finished_at'
    wait_task_terminal "$second_task"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "第二条并发任务未完成（状态: $TASK_STATE）"
    second_started="$(json_value "$TASK_RESULT" started_at)" \
        || fail '第二条并发任务缺少 started_at'
    "$PYTHON_BIN" -c '
import sys
if not float(sys.argv[1]) < float(sys.argv[2]):
    raise SystemExit(1)
' "$second_started" "$first_finished" \
        || fail '第二条任务未在第一条结束前启动，并发执行不成立'
    wait_idle_at_least "$concurrency_baseline" 45
    pass "设备 $first_id 与 $second_id 可被两个任务同时且互斥地使用"

    test_title '单任务同时申请两张指定设备卡'
    local multi_task multi_count multi_first multi_second
    submit_task_custom 'sleep 6' 0 0 0 0 GB "$first_id" "$second_id"
    multi_task="$TASK_RESULT"
    wait_task_running "$multi_task"
    mapfile -t multi_devices < <(json_array_lines "$TASK_RESULT" devices)
    multi_count="${#multi_devices[@]}"
    [[ "$multi_count" == '2' ]] \
        || fail "多卡任务应分配 2 张卡，实际为 $multi_count"
    multi_first="$(device_minor "${multi_devices[0]}")"
    multi_second="$(device_minor "${multi_devices[1]}")"
    [[ "$multi_first" == "$first_id" && "$multi_second" == "$second_id" ]] \
        || fail "多卡任务设备错配：期望 $first_id/$second_id，实际 $multi_first/$multi_second"
    [[ "$multi_first" != "$multi_second" ]] || fail '多卡任务设备列表存在重复卡号'
    refresh_status
    running_idle="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    ((running_idle <= concurrency_baseline - 2)) \
        || fail '多卡任务运行时空闲卡数量没有减少 2'
    wait_task_terminal "$multi_task"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "多卡任务未正常完成（状态: $TASK_STATE）"
    wait_idle_at_least "$concurrency_baseline" 45
    pass "单任务成功独占两张指定设备卡 $first_id/$second_id，结束后全部释放"
}
