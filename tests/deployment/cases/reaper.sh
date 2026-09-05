#!/usr/bin/env bash

run_reaper_tests() {
    if ((SKIP_DEVICE)); then
        skip '设备测试已关闭，跳过 Reaper 实测'
        return
    fi
    if ((SKIP_REAPER)); then
        skip '按参数跳过 Reaper 实测'
        return
    fi
    if ((LOCAL_WORKER == 0)); then
        skip 'Worker 不是本机地址，无法用本机 PID 验证 Reaper'
        return
    fi
    if [[ "$TEST_USER" != "$(id -un)" ]]; then
        skip '测试用户不是当前用户，无法安全创建归属匹配的本机进程'
        return
    fi
    if running_inside_neu_box_sandbox; then
        skip '当前验收脚本已处于 Neu Box sandbox 中，跳过 Reaper 实测'
        return
    fi

    refresh_status
    local reaper_baseline
    reaper_baseline="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    if ((reaper_baseline == 0)); then
        skip '当前没有空闲设备，跳过 Reaper 实测'
        return
    fi

    test_title 'Reaper 保留活跃子进程并最终回收设备'
    local go_file child_file parent_pid child_pid acquire_payload sandbox_name
    local device_count dead_parent deadline reaper_state
    go_file="$TEST_TMP/reaper.go"
    child_file="$TEST_TMP/reaper.child"
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
' "$go_file" "$child_file" &
    parent_pid=$!
    register_cleanup_pid "$parent_pid"

    acquire_payload="$(make_acquire_payload "$parent_pid")"
    http_request POST '/sandbox/acquire' "$acquire_payload"
    expect_http 201 '为 Reaper 测试申请设备失败'
    sandbox_name="$(json_value "$HTTP_BODY" sandbox_name)" \
        || fail 'acquire 响应缺少 sandbox_name'
    register_cleanup_sandbox "$sandbox_name"
    device_count="$(json_array_length "$HTTP_BODY" devices)" \
        || fail 'acquire 响应缺少 devices'
    [[ "$device_count" == '1' ]] \
        || fail "Reaper 测试应分配 1 张卡，实际为 $device_count"
    touch "$go_file"

    deadline=$((SECONDS + 15))
    while [[ ! -s "$child_file" ]] && ((SECONDS < deadline)); do
        sleep 1
    done
    [[ -s "$child_file" ]] || fail '测试父进程没有按时创建子进程'
    child_pid="$(<"$child_file")"
    [[ "$child_pid" =~ ^[1-9][0-9]*$ ]] \
        || fail "无效的测试子进程 PID: $child_pid"
    register_cleanup_pid "$child_pid"
    kill -0 "$child_pid" 2>/dev/null || fail '测试子进程未存活'

    dead_parent="$parent_pid"
    kill "$dead_parent"
    wait "$dead_parent" 2>/dev/null || true

    deadline=$((SECONDS + REAPER_TIMEOUT))
    reaper_state=''
    while ((SECONDS < deadline)); do
        http_request GET "/sandbox/list?username=$(urlencode "$TEST_USER")"
        expect_http 200 '查询 Reaper 测试沙盒失败'
        reaper_state="$(sandbox_pid_state \
            "$HTTP_BODY" "$sandbox_name" "$dead_parent" "$child_pid")" \
            || fail '无法解析沙盒 PID 状态'
        [[ "$reaper_state" == 'present:0:1' ]] && break
        sleep "$POLL_INTERVAL"
    done
    [[ "$reaper_state" == 'present:0:1' ]] \
        || fail "Reaper 未同步到存活子进程（状态: ${reaper_state:-unknown}）"
    kill -0 "$child_pid" 2>/dev/null \
        || fail '父进程退出后，活跃子进程被错误清理'
    pass '父进程退出后，Reaper 识别并保留仍存活的子进程'

    kill "$child_pid"
    deadline=$((SECONDS + REAPER_TIMEOUT))
    while ((SECONDS < deadline)); do
        http_request GET "/sandbox/list?username=$(urlencode "$TEST_USER")"
        expect_http 200 '查询 Reaper 最终回收状态失败'
        reaper_state="$(sandbox_pid_state \
            "$HTTP_BODY" "$sandbox_name" "$dead_parent" 0)" \
            || fail '无法解析沙盒最终状态'
        [[ "$reaper_state" == 'absent:0:0' ]] && break
        sleep "$POLL_INTERVAL"
    done
    [[ "$reaper_state" == 'absent:0:0' ]] \
        || fail "所有进程退出后沙盒仍未回收（状态: ${reaper_state:-unknown}）"
    wait_idle_at_least "$reaper_baseline" "$REAPER_TIMEOUT"
    pass '最后一个子进程退出后，沙盒和设备均被 Reaper 回收'
}
