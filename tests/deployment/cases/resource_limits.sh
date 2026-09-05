#!/usr/bin/env bash

run_resource_limit_tests() {
    test_title 'CPU/内存 cgroup 限制配置'
    local limit_command limit_task cpu_max memory_max
    limit_command='cg_rel=$(awk -F: '\''$1 == "0" {print $3}'\'' /proc/self/cgroup); printf "NEU_CPU_MAX="; cat "/sys/fs/cgroup${cg_rel}/cpu.max"; printf "NEU_MEMORY_MAX="; cat "/sys/fs/cgroup${cg_rel}/memory.max"'
    submit_task_custom "$limit_command" 0 0 1 128 MB
    limit_task="$TASK_RESULT"
    wait_task_terminal "$limit_task"
    [[ "$TASK_STATE" == 'completed' ]] \
        || fail "资源限制检查任务未完成（状态: $TASK_STATE）"
    http_request GET "/tasks/$limit_task/log?raw=1"
    expect_http 200 '读取资源限制任务日志失败'
    cpu_max="$(awk -F= '$1 == "NEU_CPU_MAX" {print $2; exit}' <<<"$HTTP_BODY")"
    memory_max="$(awk -F= '$1 == "NEU_MEMORY_MAX" {print $2; exit}' <<<"$HTTP_BODY")"
    [[ "$cpu_max" == '100000 100000' ]] \
        || fail "1 核任务的 cpu.max 错误: ${cpu_max:-missing}"
    [[ "$memory_max" == '134217728' ]] \
        || fail "128 MB 任务的 memory.max 错误: ${memory_max:-missing}"
    pass 'cpu.max=1 核，memory.max=128 MiB，限制写入任务实际 cgroup'

    test_title '内存上限实际阻止超额分配'
    local oom_command oom_task oom_rc
    oom_command="python3 -c 'x = bytearray(256 * 1024 * 1024); print(len(x))'"
    submit_task_custom "$oom_command" 0 0 0 128 MB
    oom_task="$TASK_RESULT"
    wait_task_terminal "$oom_task"
    [[ "$TASK_STATE" == 'failed' ]] \
        || fail '256 MiB 分配在 128 MiB 限制下仍然成功'
    oom_rc="$(json_value "$TASK_RESULT" result.returncode)" \
        || fail '超内存任务缺少 result.returncode'
    [[ "$oom_rc" =~ ^-?[0-9]+$ ]] \
        || fail "超内存任务返回码无效: $oom_rc"
    ((oom_rc != 0)) || fail '超内存任务意外返回 0'
    pass "超出 memory.max 的进程被内核终止（returncode=$oom_rc）"
}
