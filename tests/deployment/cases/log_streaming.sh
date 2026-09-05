#!/usr/bin/env bash

run_log_streaming_tests() {
    test_title '任务日志按 offset/limit 流式轮询'
    local marker_one marker_two marker_three command task_id
    local offset total response_offset chunk chunk_bytes combined
    local deadline state saw_running_chunk previous_total
    marker_one="STREAM_FIRST_$$_${RANDOM}"
    marker_two="STREAM_SECOND_$$_${RANDOM}"
    marker_three="STREAM_THIRD_$$_${RANDOM}"
    command="printf '%s\\n' '$marker_one'; sleep 3; printf '%s\\n' '$marker_two'; sleep 3; printf '%s\\n' '$marker_three'"
    submit_task "$command" 0
    task_id="$TASK_RESULT"

    offset=0
    combined=''
    previous_total=0
    saw_running_chunk=0
    deadline=$((SECONDS + TASK_TIMEOUT))
    state='queued'
    while ((SECONDS < deadline)); do
        http_request GET "/tasks/$task_id"
        expect_http 200 "查询日志流任务 $task_id 失败"
        state="$(json_value "$HTTP_BODY" status)" \
            || fail '日志流任务缺少 status'

        # 交互式 shell 的 .bashrc 可能先输出数 KB 欢迎信息；窗口过小会让
        # 轮询端落后于生产端，从而把“读得慢”误判为“没有流式写入”。
        http_request GET "/tasks/$task_id/log?offset=$offset&limit=4096"
        expect_http 200 '增量读取任务日志失败'
        response_offset="$(json_value "$HTTP_BODY" offset)" \
            || fail '日志响应缺少 offset'
        total="$(json_value "$HTTP_BODY" total_size)" \
            || fail '日志响应缺少 total_size'
        chunk="$(json_value "$HTTP_BODY" data)" \
            || fail '日志响应缺少 data'
        [[ "$response_offset" == "$offset" ]] \
            || fail "日志 offset 不连续：请求 $offset，响应 $response_offset"
        [[ "$total" =~ ^[0-9]+$ && "$response_offset" =~ ^[0-9]+$ ]] \
            || fail '日志 offset/total_size 不是非负整数'
        ((total >= previous_total)) \
            || fail "日志 total_size 倒退：$previous_total -> $total"
        previous_total="$total"

        chunk_bytes="$(json_string_utf8_length "$HTTP_BODY" data)" \
            || fail '无法计算日志 data 的字节数'
        offset=$((offset + chunk_bytes))
        combined+="$chunk"
        if [[ "$state" == 'running' && "$combined" == *"$marker_one"* ]]; then
            saw_running_chunk=1
        fi
        if [[ "$state" == 'completed' || "$state" == 'failed' ]]; then
            ((offset >= total)) && break
        fi
        sleep 1
    done

    [[ "$state" == 'completed' ]] \
        || fail "日志流任务未正常完成（状态: $state）"
    ((saw_running_chunk == 1)) \
        || fail '任务运行期间没有轮询到首段日志，日志可能不是流式写入'
    [[ "$combined" == *"$marker_one"* \
        && "$combined" == *"$marker_two"* \
        && "$combined" == *"$marker_three"* ]] \
        || fail '增量拼接后的日志缺少分段输出'
    pass '运行中日志可按字节 offset 增量轮询，offset 与 total_size 连续增长'

    test_title '任务日志 tail 与 raw 读取'
    http_request GET "/tasks/$task_id/log?tail=128"
    expect_http 200 '读取任务日志 tail 失败'
    chunk="$(json_value "$HTTP_BODY" data)" \
        || fail 'tail 日志响应缺少 data'
    [[ "$chunk" == *"$marker_three"* ]] \
        || fail 'tail 日志缺少最后一段标记'
    http_request GET "/tasks/$task_id/log?raw=1"
    expect_http 200 '读取 raw 任务日志失败'
    [[ "$HTTP_BODY" == *"$marker_one"* && "$HTTP_BODY" == *"$marker_three"* ]] \
        || fail 'raw 日志没有返回完整内容'
    pass 'tail 与 raw 两种日志读取模式均正常'
}
