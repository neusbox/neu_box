"""第 3 层 · 基本盘（manifest 1-16）。

跑在装了 worker RPM 的部署机上：真 Worker、真任务、真 cgroup。这里**没有
skip** —— 缺前置一律失败并写清缺什么，前置在 ``conftest.py`` 的组 fixture 里查。

这一组不碰卡（``device_num=0`` 的纯 CPU 任务 + 日志/参数/路由），所以它排在最
前面：它挂了说明"Worker 根本没起来"，后面那些要卡的组不用看了。
"""

from __future__ import annotations

import re
import secrets
import time

import pytest

# 会停/起 Worker 的用例都在 test_maintenance.py（整套最后），这里不再有。


def test_healthz_reports_api_version(basic):
    """1 · 健康检查与 API 版本。"""
    result = basic.client.healthz()
    assert result.status == 200, result.text
    payload = result.json()
    assert payload.get("status") == "ok", payload
    assert payload.get("role") == "worker", payload

    api_version = payload.get("api_version")
    assert isinstance(api_version, int), f"api_version 不是整数: {api_version!r}"
    assert api_version >= 2, f"Worker API 版本过旧: {api_version}（要求 >= 2）"

    version = payload.get("version")
    assert isinstance(version, str) and version.strip(), f"version 为空: {version!r}"

    schema_version = payload.get("schema_version")
    assert isinstance(schema_version, int) and schema_version >= 1, (
        f"schema_version 不是正整数: {schema_version!r}"
    )


def test_root_endpoint(basic):
    """2 · 根路径。"""
    result = basic.client.root()
    assert result.status == 200, result.text
    payload = result.json()
    assert payload.get("service") == "neuboxd", payload
    version = payload.get("version")
    assert isinstance(version, str) and version.strip(), payload
    assert version == basic.client.healthz().value("version"), (
        "/ 与 /healthz 报的版本不一致"
    )


def test_version_agrees_across_manifest_healthz_schema(basic):
    """3 · 版本三方一致：manifest.json ／ /healthz ／ DB schema 版本。"""
    manifest = basic.manifest()
    health = basic.client.healthz().json()

    for field in ("component", "version", "api_version", "schema_version",
                  "migrations"):
        assert field in manifest, f"manifest.json 缺少字段 {field!r}: {manifest}"

    assert manifest["component"] == "neuboxd", manifest
    assert manifest["version"] == health["version"], (
        f"manifest.json 版本 {manifest['version']!r} 与 /healthz 的 "
        f"{health['version']!r} 不一致（RPM 版本与 Worker 自报版本不同步）"
    )
    assert manifest["api_version"] == health["api_version"], (
        f"manifest.json api_version={manifest['api_version']!r}，"
        f"/healthz api_version={health['api_version']!r}"
    )

    migrations = manifest["migrations"]
    assert isinstance(migrations, list) and migrations, (
        f"manifest.json 的 migrations 为空: {migrations!r}"
    )
    numbers = []
    for name in migrations:
        match = re.match(r"^([0-9]{4})_", str(name))
        assert match, f"迁移文件名不合规: {name!r}"
        numbers.append(int(match.group(1)))
    assert manifest["schema_version"] == max(numbers), (
        f"manifest.json schema_version={manifest['schema_version']} "
        f"与迁移文件里的最大编号 {max(numbers)} 不一致"
    )
    assert manifest["schema_version"] == health["schema_version"], (
        f"manifest.json schema_version={manifest['schema_version']}，"
        f"但 Worker 报的库内 schema 版本是 {health['schema_version']}"
        f"（数据库没迁移到 RPM 自带的版本）"
    )


def test_status_baseline_is_self_consistent(basic):
    """4 · 资源状态基线自洽。"""
    status = basic.status()
    health = basic.client.healthz().json()

    assert status.get("status") == "online", status
    assert status.get("api_version") == health.get("api_version"), (
        f"/status api_version={status.get('api_version')!r} 与 /healthz 的 "
        f"{health.get('api_version')!r} 不一致"
    )

    total = status["total_devices"]
    idle = status["idle_devices"]
    dev_status = status["dev_status"]
    assert isinstance(total, int) and total >= 0, status
    assert isinstance(idle, int) and 0 <= idle <= total, (
        f"idle_devices={idle} 不在 0..total_devices={total} 之间"
    )
    assert isinstance(dev_status, dict), dev_status
    assert len(dev_status) == total, (
        f"total_devices={total} 与 dev_status 的条目数 {len(dev_status)} 不一致"
    )
    busy = sum(1 for value in dev_status.values() if value)
    assert idle == total - busy, (
        f"idle_devices={idle}，但 dev_status 里有 {busy} 张忙、共 {total} 张"
    )

    assert isinstance(status.get("active_sandboxes"), int), status
    assert status["active_sandboxes"] >= 0, status

    for field in ("total_cpu", "idle_cpu", "total_mem", "idle_mem"):
        assert field in status, f"/status 缺少 {field!r}: {status}"
    assert status["total_cpu"] > 0, f"total_cpu={status['total_cpu']}"
    assert 0.0 <= status["idle_cpu"] <= 100.0, f"idle_cpu={status['idle_cpu']}"
    assert status["total_mem"] > 0, f"total_mem={status['total_mem']}"
    assert 0 <= status["idle_mem"] <= status["total_mem"], status

    maintenance = status.get("maintenance")
    assert isinstance(maintenance, dict), maintenance
    assert maintenance.get("paused") is False, (
        f"/status 里 maintenance.paused={maintenance.get('paused')!r}；"
        f"验收要求 Worker 处于正常调度状态"
    )


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/command/queue"),
        ("POST", "/sandbox/gate"),
        ("POST", "/sandbox/container"),
    ],
)
def test_removed_routes_are_gone(basic, method, path):
    """5 · 已删除的路由确实关了。"""
    result = basic.client.request(
        method, path, payload={} if method == "POST" else None,
    )
    assert result.status == 404, (
        f"{method} {path} 返回 HTTP {result.status}，应为 404；"
        f"旧架构的路由又被注册回来了:\n{result.text[:500]}"
    )
    # 反向对照：新路由必须是活的，避免"整站 404"被误判为通过。
    assert basic.client.list_tasks().status == 200


def test_submit_with_unknown_user_is_rejected(basic):
    """6 · 不存在的用户被拒。"""
    missing = f"neu_box_missing_{secrets.token_hex(4)}"
    payload = {
        "user_id": missing, "command": "true", "device_num": 0,
        "cpu": 0, "memory": 0, "mem_unit": "GB",
    }
    result = basic.client.create_task(payload)
    assert result.status == 400, (
        f"不存在的用户 {missing} 没有被拒绝（HTTP {result.status}）:\n"
        f"{result.text[:500]}"
    )
    error = result.value("error")
    assert missing in error and "不存在" in error, (
        f"错误信息没有说清是用户不存在: {error!r}"
    )


def test_unknown_task_id_returns_404(basic):
    """7 · 不存在的任务 ID。"""
    missing = secrets.token_hex(6)
    result = basic.client.task(missing)
    assert result.status == 404, (
        f"不存在的任务 {missing} 返回 HTTP {result.status}，应为 404:\n"
        f"{result.text[:500]}"
    )
    assert "error" in result.json(), result.text
    assert result.json()["error"], "404 响应里的 error 为空"


def test_zero_device_task_runs_and_logs(basic):
    """8 · 零设备任务跑通：提交 + 等终态 + 读日志。"""
    marker = f"neu-box-smoke-{secrets.token_hex(4)}"
    task_id = basic.submit(f"printf '%s\\n' {marker!r}", device_num=0)
    task = basic.wait_task(task_id)

    assert task["status"] == "completed", (
        f"零设备任务 {task_id} 状态为 {task['status']}，"
        f"result={task.get('result')}\n日志:\n{basic.task_log_text(task_id)[:2000]}"
    )
    assert task["result"]["returncode"] == 0, task["result"]
    assert task["devices"] == [], f"零设备任务却分配了设备: {task['devices']}"
    assert marker in basic.task_log_text(task_id), "任务日志缺少预期标记"


def test_shell_syntax_error_lands_in_log(basic):
    """9 · Shell 语法错误进日志。"""
    task_id = basic.submit("if", device_num=0)
    task = basic.wait_task(task_id)

    assert task["status"] == "failed", (
        f"语法错误任务状态应为 failed，实际为 {task['status']}；"
        f"result={task.get('result')}"
    )
    assert task["result"]["returncode"] != 0, task["result"]
    text = basic.task_log_text(task_id)
    assert re.search(r"syntax error|unexpected end|语法错误", text, re.I), (
        f"任务日志没有返回 Shell 解析错误信息:\n{text[:2000]}"
    )


def _read_all_chunks(deployment, task_id: str, window: int = 4096):
    """按 offset/limit 轮询到任务结束，返回 (拼起来的日志, 观察到的状态集合)。"""
    offset = 0
    previous_total = 0
    combined = ""
    states = []
    saw_running_chunk = False
    deadline = time.time() + deployment.task_timeout

    while time.time() < deadline:
        status = deployment.get_task(task_id)["status"]
        states.append(status)

        result = deployment.client.task_log(task_id, offset=offset, limit=window)
        assert result.status == 200, result.text
        payload = result.json()
        for field in ("data", "offset", "total_size"):
            assert field in payload, f"日志响应缺少 {field!r}: {payload}"
        assert payload["offset"] == offset, (
            f"日志 offset 不连续：请求 {offset}，响应 {payload['offset']}"
        )
        total = payload["total_size"]
        assert isinstance(total, int) and total >= previous_total, (
            f"日志 total_size 倒退：{previous_total} → {total}"
        )
        previous_total = total

        chunk = payload["data"]
        offset += len(chunk.encode("utf-8"))
        combined += chunk
        if status == "running" and combined:
            saw_running_chunk = True
        if status in {"completed", "failed"} and offset >= total:
            break
        time.sleep(deployment.poll)
    else:
        pytest.fail(
            f"任务 {task_id} 的日志在 {deployment.task_timeout:.0f}s 内没有读完"
            f"（offset={offset}, total={previous_total}）",
            pytrace=False,
        )

    return combined, states, saw_running_chunk


def test_log_streaming_by_offset_and_limit(basic):
    """10 · 日志 offset/limit 流式。"""
    first = f"STREAM_FIRST_{secrets.token_hex(4)}"
    second = f"STREAM_SECOND_{secrets.token_hex(4)}"
    third = f"STREAM_THIRD_{secrets.token_hex(4)}"
    command = (
        f"printf '%s\\n' {first!r}; sleep 2; "
        f"printf '%s\\n' {second!r}; sleep 2; "
        f"printf '%s\\n' {third!r}"
    )
    task_id = basic.submit(command, device_num=0)
    combined, states, saw_running_chunk = _read_all_chunks(basic, task_id)

    assert states[-1] == "completed", (
        f"日志流任务最终状态为 {states[-1]}，日志:\n{combined[:2000]}"
    )
    assert saw_running_chunk, "任务运行期间没有轮询到任何日志，日志可能不是流式写入"
    for marker in (first, second, third):
        assert marker in combined, (
            f"增量拼接后的日志缺少 {marker!r}：\n{combined[:2000]}"
        )


def test_log_tail_and_raw(basic):
    """11 · 日志 tail / raw 两种读法。"""
    marker = f"TAIL_{secrets.token_hex(4)}"
    task_id = basic.submit(
        f"printf '%s\\n' {marker!r}; printf 'x%.0s' $(seq 1 512)", device_num=0,
    )
    basic.wait_task(task_id)

    full = basic.task_log_text(task_id)
    assert marker in full, full

    raw = basic.client.task_log(task_id, raw=1)
    assert raw.status == 200, raw.text
    assert raw.text == full, "raw 读法与整段读法返回的内容不一致"

    tail = basic.client.task_log(task_id, tail=128)
    assert tail.status == 200, tail.text
    payload = tail.json()
    assert payload["data"] == full.encode("utf-8")[-128:].decode("utf-8", "replace"), (
        "tail=128 返回的不是日志末尾 128 字节"
    )
    assert payload["total_size"] == len(full.encode("utf-8")), payload

    # 取末尾一段必须只是"一段"：比整个文件短才算真的按 tail 截断。
    assert len(payload["data"].encode("utf-8")) <= 128, payload
    assert len(full.encode("utf-8")) > 128, "样本日志太短，tail 断言没有意义"


def test_log_offset_beyond_end_is_clamped(basic):
    """12 · 日志 offset 越界。"""
    task_id = basic.submit("printf 'hello\\n'", device_num=0)
    basic.wait_task(task_id)

    total = basic.client.task_log(task_id).value("total_size")
    assert isinstance(total, int) and total > 0, f"日志长度异常: {total}"

    result = basic.client.task_log(task_id, offset=total + 4096)
    assert result.status == 200, (
        f"越界 offset 让读取失败了（HTTP {result.status}），越界应当是正常返回空:\n"
        f"{result.text[:500]}"
    )
    payload = result.json()
    assert payload["data"] == "", f"越界 offset 仍返回了内容: {payload['data']!r}"
    assert payload["offset"] == total, (
        f"越界 offset 没有被夹到文件长度：请求 {total + 4096}，"
        f"响应 offset={payload['offset']}，total_size={payload['total_size']}"
    )
    assert payload["total_size"] == total, payload


def test_priority_bounds(basic):
    """13 · 优先级参数边界。"""
    for invalid in (-1, 2):
        payload = basic.task_payload("true", device_num=0, priority=invalid)
        result = basic.client.create_task(payload)
        assert result.status == 400, (
            f"priority={invalid} 没有被拒绝（HTTP {result.status}）:\n"
            f"{result.text[:500]}"
        )
        error = result.value("error")
        assert "priority" in error.lower(), (
            f"priority={invalid} 的错误信息不明确: {error!r}"
        )

    for valid in (0, 1):
        task_id = basic.submit("true", device_num=0, priority=valid)
        task = basic.wait_task(task_id)
        assert task["status"] == "completed", task
        assert task["priority"] == valid, (
            f"priority={valid} 的任务入库后 priority={task['priority']}"
        )


def test_delete_completed_task_and_log(basic):
    """14 · 已完成任务与日志删除。"""
    task_id = basic.submit("printf 'delete-me\\n'", device_num=0)
    task = basic.wait_task(task_id)
    assert task["status"] == "completed", task

    result = basic.client.delete_tasks([task_id])
    assert result.status == 200, result.text
    assert result.value("deleted") == 1, result.text

    assert basic.client.task(task_id).status == 404, "已删除的任务仍可查询"
    log = basic.client.task_log(task_id, raw=1)
    assert log.status == 200, log.text
    assert log.text == "", f"删除任务后日志文件仍有内容: {log.text[:200]!r}"


def test_delete_queued_task_never_runs(single_card):
    """15 · 排队任务删除后不执行（1 卡）。"""
    baseline = single_card.idle_devices()
    device = single_card.idle_minors()[0]

    blocker = single_card.submit("sleep 60", device_ids=[device])
    single_card.wait_task_running(blocker)

    marker = f"SHOULD_NOT_RUN_{secrets.token_hex(4)}"
    queued = single_card.submit(f"printf '%s\\n' {marker!r}", device_ids=[device])
    state = single_card.get_task(queued)["status"]
    assert state == "queued", (
        f"同一张卡上的第二个任务没有保持 queued（状态 {state}）；"
        f"设备 {device} 应被 blocker 占满"
    )

    result = single_card.client.delete_tasks([queued])
    assert result.status == 200, result.text
    assert result.value("deleted") == 1, result.text

    assert single_card.client.task(queued).status == 404, "排队任务删除后仍可查询"
    assert marker not in single_card.task_log_text(queued), "已删除的排队任务仍被执行"

    single_card.client.delete_tasks([blocker])
    single_card.wait_task(blocker)  # 取消是异步的，等它落到终态
    single_card.wait_idle_at_least(baseline)


def test_cgroup_cpu_and_memory_limits(basic):
    """16 · cgroup CPU/内存上限写入。"""
    command = (
        "cg_rel=$(awk -F: '$1 == \"0\" {print $3}' /proc/self/cgroup); "
        "printf 'NEU_CPU_MAX='; cat \"/sys/fs/cgroup${cg_rel}/cpu.max\"; "
        "printf 'NEU_MEMORY_MAX='; cat \"/sys/fs/cgroup${cg_rel}/memory.max\""
    )
    task_id = basic.submit(command, device_num=0, cpu=1, memory=128, mem_unit="MB")
    task = basic.wait_task(task_id)
    assert task["status"] == "completed", (
        f"资源限制检查任务未完成（状态 {task['status']}）:\n"
        f"{basic.task_log_text(task_id)[:2000]}"
    )

    text = basic.task_log_text(task_id)
    cpu_max = _log_value(text, "NEU_CPU_MAX")
    memory_max = _log_value(text, "NEU_MEMORY_MAX")
    assert cpu_max == "100000 100000", (
        f"1 核任务的 cpu.max 应为 '100000 100000'，实际为 {cpu_max!r}；"
        f"任务没有跑在自己的沙盒 cgroup 里，或限制没写进去"
    )
    assert memory_max == "134217728", (
        f"128 MB 任务的 memory.max 应为 134217728，实际为 {memory_max!r}"
    )


def _log_value(text: str, key: str) -> str:
    match = re.search(rf"^{key}=(.*)$", text, re.M)
    assert match, f"任务日志里没有 {key}:\n{text[:2000]}"
    return match.group(1).strip()
