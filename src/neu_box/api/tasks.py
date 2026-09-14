"""命令任务 HTTP API；调度实现位于 :mod:`task_queue`。"""

from __future__ import annotations

import pwd
import sqlite3

from flask import Blueprint, request

from neu_box.execution.target import (
    TARGET_DOCKER,
    normalize_execution_target,
    public_execution_target,
    TargetValidationError,
)
from neu_box.storage import Database
from neu_box.runtime import devices
from neu_box.runtime.sandbox import SandboxAllocationPaused, SbxManager
from neu_box.scheduling.queue import TaskQueue
from neu_box.execution import logs

command_bp = Blueprint('command', __name__)


def _normalize_device_ids(raw: list, all_devices: list[str]) -> list[str] | None:
    by_minor = {device.split(':', 1)[1]: device for device in all_devices}
    result = []
    for value in raw:
        value = str(value).strip()
        device = value if value in all_devices else by_minor.get(value)
        if device is None:
            return None
        if device not in result:
            result.append(device)
    return result or None


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


@command_bp.route('', methods=['POST'])
def create_task():
    body = request.get_json(silent=True) or {}
    command = (body.get('command') or '').strip()
    user_id = (body.get('user_id') or '').strip()
    if not command:
        return {'error': '命令不能为空'}, 400
    if not user_id:
        return {'error': 'user_id 不能为空'}, 400
    try:
        pwd.getpwnam(user_id)
    except KeyError:
        return {'error': f'系统用户 {user_id} 不存在'}, 400

    cpu = body.get('cpu', 0)
    memory = body.get('memory', 0)
    device_num = body.get('device_num', 0)
    if not isinstance(cpu, int) or cpu < 0:
        return {'error': 'cpu 必须是非负整数'}, 400
    if not isinstance(memory, int) or memory < 0:
        return {'error': 'memory 必须是非负整数'}, 400
    if not isinstance(device_num, int) or device_num < 0:
        return {'error': 'device_num 必须是非负整数'}, 400
    mem_unit = str(body.get('mem_unit', 'GB')).upper()
    if mem_unit not in {'GB', 'MB'}:
        return {'error': 'mem_unit 必须是 GB 或 MB'}, 400
    mem = '0' if memory == 0 else f'{memory}{"G" if mem_unit == "GB" else "M"}'

    raw_ids = body.get('device_ids')
    sbx = SbxManager.get_instance()
    all_devices = devices.discover_nodes()
    if not raw_ids and device_num > len(all_devices):
        return {'error': f'设备不足: 需要 {device_num} 个, 系统共 {len(all_devices)} 个'}, 400
    normalized_ids = None
    if raw_ids:
        if not isinstance(raw_ids, list):
            return {'error': 'device_ids 必须是数组'}, 400
        normalized_ids = _normalize_device_ids(raw_ids, all_devices)
        if normalized_ids is None:
            return {'error': f'device_ids 包含不存在的设备: {raw_ids}'}, 400
    try:
        target = normalize_execution_target(body.get('target'))
    except TargetValidationError as exc:
        return {'error': str(exc)}, 400
    if target['type'] == TARGET_DOCKER and not (normalized_ids or device_num > 0):
        return {'error': 'docker 目标必须通过 device_ids 或 device_num 申请至少一张设备'}, 400
    est_time = body.get('est_time', 0)
    if not isinstance(est_time, int) or est_time < 0:
        est_time = 0
    try:
        task_id = TaskQueue.get_instance().submit(
            user_id, command, cpu, mem,
            0 if normalized_ids else device_num,
            normalized_ids, target, est_time, body.get('priority', 0),
        )
    except SandboxAllocationPaused as exc:
        return {'error': str(exc), 'code': 'worker_paused'}, 503
    except (ValueError, sqlite3.IntegrityError) as exc:
        return {'error': str(exc)}, 400
    queue = TaskQueue.get_instance()
    task = Database.get_instance().get_task(task_id)
    position = queue.position(task_id)
    return {
        'task_id': task_id,
        'position': position,
        'priority': task.get('priority', 0) if task else 0,
        'target': public_execution_target(target),
        'message': (
            f'任务已提交，队列位置 #{position}' if position
            else '任务已提交，正在调度'
        ),
    }, 202


@command_bp.route('', methods=['DELETE'])
def delete_tasks():
    body = request.get_json(silent=True) or {}
    task_ids = body.get('task_ids') or []
    if not task_ids:
        return {'error': 'task_ids 不能为空'}, 400
    deleted = TaskQueue.get_instance().delete_tasks(task_ids)
    return {'deleted': deleted, 'message': f'已删除 {deleted} 个任务'}, 200


@command_bp.route('', methods=['GET'])
def list_tasks():
    queue = TaskQueue.get_instance()
    return {'queue': queue.get_queue(), 'total_pending': queue.pending_count()}, 200


@command_bp.route('/<task_id>', methods=['GET'])
def get_task(task_id: str):
    result = TaskQueue.get_instance().get_result(task_id)
    return (result, 200) if result is not None else ({'error': '任务不存在'}, 404)


@command_bp.route('/<task_id>/log', methods=['GET'])
def get_task_log(task_id: str):
    payload = logs.read_log(
        task_id,
        offset=_parse_int(request.args.get('offset'), 0),
        limit=_parse_int(request.args.get('limit'), 0),
        tail=_parse_int(request.args.get('tail'), 0),
    )
    if 'error' in payload:
        return payload, 500
    if request.args.get('raw'):
        return payload['data'], 200, {'Content-Type': 'text/plain; charset=utf-8'}
    return payload, 200
