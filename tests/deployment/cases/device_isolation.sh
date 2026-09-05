#!/usr/bin/env bash

run_device_isolation_tests() {
    if ((SKIP_DEVICE)); then
        skip '按参数跳过设备权限隔离测试'
        return
    fi
    if ((LOCAL_WORKER == 0)); then
        skip 'Worker 不是本机地址，无法解析设备节点并验证沙盒外直接访问'
        return
    fi
    if [[ "$TEST_USER" != "$(id -un)" ]]; then
        skip '测试用户不是当前用户，无法对比沙盒内外的设备 open 权限'
        return
    fi
    if running_inside_neu_box_sandbox; then
        skip '当前验收脚本已在 Neu Box sandbox 中，无法作为沙盒外对照组'
        return
    fi

    refresh_status
    local isolation_baseline
    isolation_baseline="$(json_value "$HTTP_BODY" idle_devices)" \
        || fail 'status 缺少 idle_devices'
    if ((isolation_baseline < 2)); then
        skip '设备权限隔离需要至少 2 张空闲卡'
        return
    fi

    local -a idle_ids
    mapfile -t idle_ids < <(idle_device_ids "$HTTP_BODY")
    local assigned_id blocked_id assigned_node blocked_node
    assigned_id="${idle_ids[0]}"
    blocked_id="${idle_ids[1]}"
    assigned_node="$(device_node_for_minor "$assigned_id" || true)"
    blocked_node="$(device_node_for_minor "$blocked_id" || true)"
    if [[ -z "$assigned_node" || -z "$blocked_node" ]]; then
        skip '未在本机找到对应的 nvidiaN/davinciN 字符设备节点'
        return
    fi
    if ! can_open_device "$assigned_node" || ! can_open_device "$blocked_node"; then
        skip '设备节点在未分配时也无法由测试用户打开，无法排除 Unix 权限或驱动错误'
        return
    fi

    test_title '设备权限隔离与沙盒外绕过阻断'
    local python_code quoted_code command task_id allocated_minor
    local -a allocated_devices
    python_code='import errno, os, sys
assigned, blocked = sys.argv[1:]
fd = os.open(assigned, os.O_RDWR | os.O_NONBLOCK)
os.close(fd)
print("INSIDE_ASSIGNED_OPEN_OK", flush=True)
try:
    fd = os.open(blocked, os.O_RDWR | os.O_NONBLOCK)
except OSError as exc:
    if exc.errno not in (errno.EPERM, errno.EACCES):
        raise
    print("INSIDE_UNASSIGNED_BLOCKED", flush=True)
else:
    os.close(fd)
    print("UNASSIGNED_DEVICE_WAS_OPENED", flush=True)
    raise SystemExit(42)
print("ISOLATION_READY", flush=True)'
    quoted_code="$("$PYTHON_BIN" -c \
        'import shlex, sys; print(shlex.quote(sys.stdin.read()))' \
        <<<"$python_code")"
    command="python3 -c $quoted_code '$assigned_node' '$blocked_node'; rc=\$?; sleep 8; exit \"\$rc\""
    submit_task "$command" 0 0 "$assigned_id"
    task_id="$TASK_RESULT"
    wait_task_running "$task_id"
    wait_log_contains "$task_id" 'ISOLATION_READY' 20

    # 此时 assigned_node 已由任务沙盒预留；根 cgroup 中的新进程必须被拒绝。
    if ! "$PYTHON_BIN" -c '
import errno
import os
import sys
try:
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_NONBLOCK)
except OSError as exc:
    raise SystemExit(0 if exc.errno in (errno.EPERM, errno.EACCES) else 2)
else:
    os.close(fd)
    raise SystemExit(1)
' "$assigned_node"; then
        fail "沙盒外进程仍能打开已预留设备 $assigned_node，或返回了非权限类错误"
    fi

    wait_task_terminal "$task_id"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "设备隔离任务未正常完成（状态: $TASK_STATE）"
    mapfile -t allocated_devices < <(json_array_lines "$TASK_RESULT" devices)
    [[ "${#allocated_devices[@]}" == '1' ]] \
        || fail '设备隔离任务没有恰好分配一张卡'
    allocated_minor="$(device_minor "${allocated_devices[0]}")"
    [[ "$allocated_minor" == "$assigned_id" ]] \
        || fail "指定设备 $assigned_id 实际分配成 $allocated_minor"
    http_request GET "/tasks/$task_id/log?raw=1"
    expect_http 200 '读取设备隔离任务日志失败'
    [[ "$HTTP_BODY" == *'INSIDE_ASSIGNED_OPEN_OK'* \
        && "$HTTP_BODY" == *'INSIDE_UNASSIGNED_BLOCKED'* ]] \
        || fail '沙盒内设备 open 权限结果不完整'
    wait_idle_at_least "$isolation_baseline" 45
    pass "沙盒内仅卡 $assigned_id 可打开，卡 $blocked_id 被拒绝；沙盒外也无法绕过预留"
}
