"""Task 的形状：沙盒命名、排序键、对外表示。

队列顺序全部由这几个纯函数算出来 —— 它们不碰调度状态（状态留在
``queue.py`` 的 ``TaskQueue`` 上），所以谁都能直接调用、也能单独测。
"""

from __future__ import annotations

from neu_box.execution.target import public_execution_target


def sandbox_name(task: dict) -> str:
    """命令任务的沙盒名是确定性的：重启恢复靠它把任务和 cgroup 对上。"""
    return f"sbx_{task['user_id']}_{task['task_id']}.slice"


def sort_key(task: dict) -> tuple:
    """排队顺序：优先级 DESC → FIFO。

    ``priority`` 取值 0（普通）或 1（赶论文），由数据层校验；数字越大越先执行，
    同级内按提交时间先到先执行。

    这个键只用于**展示顺序**（``get_queue`` / ``position``）和**重启恢复时的
    入队顺序** —— 真正的调度顺序由 ``TaskQueue._queues`` 的两个桶决定，两者必须
    一致，改一个记得改另一个。
    """
    return (
        -(task.get('priority', 0) or 0),
        task.get('created_at') or 0,
        task['task_id'],
    )


def public(task: dict) -> dict:
    """任务对外的表示（HTTP 响应里那一个）。"""
    target = task.get('target') or task.get('target_spec')
    return {
        'task_id': task['task_id'], 'user_id': task['user_id'],
        'command': task['command'], 'status': task['status'],
        'position': task.get('position', 0),
        'priority': task.get('priority', 0) or 0,
        'cpu': task.get('cpu', 0), 'est_time': task.get('est_time', 0) or 0,
        'eta': task.get('eta'), 'mem': task.get('mem', '0'),
        'device_num': task.get('device_num', len(task.get('devices') or [])),
        'devices': task.get('devices', []),
        'target': public_execution_target(target),
        'created_at': task.get('created_at'),
        'started_at': task.get('started_at'),
        'finished_at': task.get('finished_at'),
    }
