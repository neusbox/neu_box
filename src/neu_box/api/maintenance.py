"""Worker maintenance control HTTP API."""

from flask import Blueprint, request

from neu_box.scheduling.queue import TaskQueue

maintenance_bp = Blueprint('maintenance', __name__)


def _require_local():
    if request.remote_addr not in {'127.0.0.1', '::1'}:
        return {'error': '维护控制接口仅允许本机访问'}, 403
    return None


@maintenance_bp.route('/maintenance', methods=['GET'])
def maintenance_status():
    return {'maintenance': TaskQueue.get_instance().maintenance_status()}, 200


@maintenance_bp.route('/maintenance/pause', methods=['POST'])
def pause_worker():
    denied = _require_local()
    if denied is not None:
        return denied
    # 显式 pause 请求 = 一次停机维护开始。此后 resume 会被拒绝，直到服务
    # 重启（见 TaskQueue.set_paused）。
    return {
        'message': 'Worker 已暂停接收新任务和分配沙盒',
        'maintenance': TaskQueue.get_instance().set_paused(
            True, maintenance_request=True,
        ),
    }, 200


@maintenance_bp.route('/maintenance/resume', methods=['POST'])
def resume_worker():
    denied = _require_local()
    if denied is not None:
        return denied
    queue = TaskQueue.get_instance()
    if queue.maintenance_in_progress():
        # 已暂停但还没停服：这时候恢复调度会让 pause 停下来等的那一堆状态
        # 重新动起来，维护的前提就没了。直接拒绝，不做"中止本次维护"这种处理。
        return {
            'error': (
                '暂停维护正在进行（已排空、尚未停服），不能恢复调度；'
                '请等待 pause 完成，或重启服务后再 resume'
            ),
            'code': 'maintenance_in_progress',
        }, 409
    return {
        'message': 'Worker 已恢复接收新任务和分配沙盒',
        'maintenance': queue.set_paused(False),
    }, 200
