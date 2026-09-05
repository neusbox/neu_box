#!/usr/bin/env bash

run_sandbox_lifecycle_tests() {
    if ((LOCAL_WORKER == 0)); then
        skip 'Worker 不是本机地址，无法用本机 PID 验证显式沙盒生命周期'
        return
    fi
    if [[ "$TEST_USER" != "$(id -un)" ]]; then
        skip '测试用户不是当前用户，无法安全创建归属匹配的本机进程'
        return
    fi
    if running_inside_neu_box_sandbox; then
        skip '当前验收脚本已在 Neu Box sandbox 中，跳过显式 acquire/join/release'
        return
    fi

    refresh_status
    local lifecycle_baseline device_id=''
    lifecycle_baseline="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    if ((SKIP_DEVICE == 0 && lifecycle_baseline > 0)); then
        local -a idle_ids
        mapfile -t idle_ids < <(idle_device_ids "$HTTP_BODY")
        device_id="${idle_ids[0]}"
    fi

    test_title '显式 acquire / join / list / release 沙盒生命周期'
    local first_pid second_pid acquire_payload sandbox_name device_count
    local join_payload list_state release_payload first_cgroup second_cgroup
    first_pid="$(start_detached_sleep)"
    register_cleanup_pid "$first_pid"
    second_pid="$(start_detached_sleep)"
    register_cleanup_pid "$second_pid"

    if [[ -n "$device_id" ]]; then
        acquire_payload="$(make_acquire_payload "$first_pid" 0 0 0 GB "$device_id")"
    else
        acquire_payload="$(make_acquire_payload "$first_pid" 0 0 0 GB)"
    fi
    http_request POST '/sandbox/acquire' "$acquire_payload"
    expect_http 201 '显式 acquire 沙盒失败'
    sandbox_name="$(json_value "$HTTP_BODY" sandbox_name)" \
        || fail 'acquire 响应缺少 sandbox_name'
    [[ -n "$sandbox_name" ]] || fail 'acquire 返回空 sandbox_name'
    register_cleanup_sandbox "$sandbox_name"
    device_count="$(json_array_length "$HTTP_BODY" devices)" \
        || fail 'acquire 响应缺少 devices'
    if [[ -n "$device_id" ]]; then
        [[ "$device_count" == '1' ]] \
            || fail "显式沙盒应分配一张卡，实际为 $device_count"
    else
        [[ "$device_count" == '0' ]] \
            || fail "零设备显式沙盒意外分配了 $device_count 张卡"
    fi

    http_request GET "/sandbox/list?username=$(urlencode "$TEST_USER")"
    expect_http 200 'acquire 后查询沙盒失败'
    list_state="$(sandbox_pid_state "$HTTP_BODY" "$sandbox_name" "$first_pid" "$second_pid")" \
        || fail '无法解析 acquire 后的沙盒 PID'
    [[ "$list_state" == 'present:1:0' ]] \
        || fail "acquire 后 PID 列表错误: $list_state"

    join_payload="$(make_join_payload "$second_pid" "$sandbox_name")"
    http_request POST '/sandbox/join' "$join_payload"
    expect_http 200 '将第二个 PID join 到沙盒失败'
    http_request GET "/sandbox/list?username=$(urlencode "$TEST_USER")"
    expect_http 200 'join 后查询沙盒失败'
    list_state="$(sandbox_pid_state "$HTTP_BODY" "$sandbox_name" "$first_pid" "$second_pid")" \
        || fail '无法解析 join 后的沙盒 PID'
    [[ "$list_state" == 'present:1:1' ]] \
        || fail "join 后 PID 列表错误: $list_state"
    first_cgroup="$(awk -F: '$1 == "0" {print $3}' "/proc/$first_pid/cgroup")"
    second_cgroup="$(awk -F: '$1 == "0" {print $3}' "/proc/$second_pid/cgroup")"
    [[ -n "$first_cgroup" && "$first_cgroup" == "$second_cgroup" \
        && "$first_cgroup" == *"sandbox_$sandbox_name"* ]] \
        || fail "两个 PID 没有进入同一沙盒 cgroup：$first_cgroup / $second_cgroup"

    release_payload="$(make_release_payload "$sandbox_name")"
    http_request POST '/sandbox/release' "$release_payload"
    expect_http 200 '显式 release 沙盒失败'
    wait_process_gone "$first_pid" 20
    wait_process_gone "$second_pid" 20
    http_request GET "/sandbox/list?username=$(urlencode "$TEST_USER")"
    expect_http 200 'release 后查询沙盒失败'
    list_state="$(sandbox_pid_state "$HTTP_BODY" "$sandbox_name" "$first_pid" "$second_pid")" \
        || fail '无法解析 release 后的沙盒状态'
    [[ "$list_state" == 'absent:0:0' ]] \
        || fail "release 后沙盒仍存在: $list_state"
    if [[ -n "$device_id" ]]; then
        wait_idle_at_least "$lifecycle_baseline" 45
    fi
    pass "显式沙盒完成创建、追加 PID、列举和销毁；release 杀死全部残留进程${device_id:+并释放设备 $device_id}"
}
