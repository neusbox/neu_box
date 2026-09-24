"""容器归属登记端点。

路径按**资源**命名（``/container/register``），不按调用方 —— 调它的是节点上的
OCI runtime hook，但这里管的是容器，不是 runtime。

The hook calls it from Docker's create path.  It must never query Docker: doing
so from that path would re-enter Docker authorization plugins.  The hook
supplies the trusted container PID/ID and the sandbox name carried by the
``sandbox_cgroup`` annotation; Worker derives the mount namespace and cgroup
values directly from ``/proc``.

The endpoint is the Worker half of a cross-repo contract: the wire format is
frozen in ``docs/worker-api.md``, the Worker-side flow is in
``docs/container-registration.md``, and the runtime half (wrapper, hook) lives
in the neu_box_runtime repository.
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, request

# pid → 沙盒、pid 属主校验和沙盒属主解析都只有一份实现，就在 sandboxes.py：
# acquire / join / status 用的也是它们，借条接口必须和它们口径一致。
from neu_box.api.sandboxes import (
    _find_sandbox_for_pid,
    _sandbox_owner,
    _verify_pid_owner,
)
from neu_box.runtime.container_intents import START_INTENT_TTL, StartIntentStore
from neu_box.runtime.containers import (
    DockerExecutorError,
    runtime_container_identity,
)
from neu_box.runtime.sandbox import SbxManager

logger = logging.getLogger(__name__)

container_bp = Blueprint("container", __name__)

# 借条按容器 ID 记（Docker 的 64 位十六进制 ID）：hook 报上来的、docker inspect
# 拿到的都是它，容器名会变、ID 不会。
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


def _sandbox_name_from_annotation(value: str) -> str | None:
    """Resolve the ``sandbox_cgroup`` annotation to a stored sandbox name.

    The annotation carries the sandbox name verbatim, so only an exact match
    against the database key is accepted.  cgroup paths and bare basenames are
    rejected on purpose: those are recycled, and after a sandbox is destroyed
    and recreated the stale annotation would point at a live sandbox that
    belongs to somebody else.
    """
    name = str(value or "").strip()
    if not name:
        return None
    if not SbxManager.get_instance().db.get_sandbox(name):
        return None
    return name


def _runtime_error_response(exc: DockerExecutorError):
    """Map a registration failure to the contract's status codes."""
    if exc.code == "sandbox_not_found":
        return {"error": str(exc), "code": exc.code}, 404
    return {"error": str(exc), "code": exc.code}, 409


def _intent_request_fields(body: dict):
    """借条接口共用的三个字段校验；不合法时返回 (None, 错误响应)。"""
    username = str(body.get("username") or "").strip()
    container_id = str(body.get("container_id") or "").strip().lower()
    if not username or not container_id:
        return None, ({"error": "username 和 container_id 为必填参数"}, 400)
    if not _CONTAINER_ID.fullmatch(container_id):
        return None, ({"error": "container_id 必须是 64 位十六进制容器 ID"}, 400)
    try:
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        return None, ({"error": "pid 必须为正整数"}, 400)
    if pid <= 0:
        return None, ({"error": "pid 必须为正整数"}, 400)
    if not _verify_pid_owner(pid, username):
        return None, ({
            "error": f"PID {pid} 不属于用户 {username}，或进程不存在",
            "code": "pid_owner_mismatch",
        }, 409)
    return (username, container_id, pid), None


@container_bp.post("/intent")
def lend_sandbox_for_start():
    """存一张 start 借条：把 ``pid`` 所在的那个沙盒借给 ``container_id``。

    ``neubox docker start`` 在真 start 之前调它。借条不能凭空借出卡：pid 必须是
    调用方自己的（``/proc/<pid>/status`` 校属主），pid 必须真的在某个沙盒里，而
    且那个沙盒的属主就是调用方。谁能借、借给谁、认领不到怎么办，见
    ``docs/container-registration.md``。
    """
    body = request.get_json(silent=True) or {}
    fields, error = _intent_request_fields(body)
    if error is not None:
        return error
    username, container_id, pid = fields

    sandbox_name = _find_sandbox_for_pid(pid)
    if not sandbox_name:
        return {
            "error": f"PID {pid} 不在任何沙盒中；请先在本 shell 执行 neubox acquire",
            "code": "not_in_sandbox",
        }, 409
    if _sandbox_owner(sandbox_name) != username:
        return {
            "error": f"沙盒 {sandbox_name} 不属于用户 {username}",
            "code": "sandbox_owner_mismatch",
        }, 409
    sandbox = SbxManager.get_instance().db.get_sandbox(sandbox_name)
    if not sandbox or sandbox.get('state') == 'DESTROYING':
        return {
            "error": f"沙盒 {sandbox_name} 当前不可用",
            "code": "sandbox_not_active",
        }, 409

    StartIntentStore.get_instance().lend(container_id, username, sandbox_name)
    logger.warning(
        "已记录 start 借条：容器 %s（属主 %s）→ 沙盒 '%s'，%.0fs 内有效",
        container_id, username, sandbox_name, START_INTENT_TTL,
    )
    return {
        "container_id": container_id,
        "sandbox_name": sandbox_name,
        "owner": username,
        "state": "pending",
        "expires_in": START_INTENT_TTL,
    }, 200


@container_bp.get("/intent")
def start_intent_state():
    """查一张借条有没有被认领：``neubox docker start`` 用它确认卡真的借出去了。

    ``neubox`` 启动完容器后轮询这里；``state`` 为 ``consumed`` 才算绑上沙盒。
    没有借条时返回 ``state: null``（不是 404：这里只回答"有没有"）。
    """
    body = {
        "username": request.args.get("username"),
        "container_id": request.args.get("container_id"),
        "pid": request.args.get("pid"),
    }
    fields, error = _intent_request_fields(body)
    if error is not None:
        return error
    username, container_id, _pid = fields

    entry = StartIntentStore.get_instance().peek(container_id, username)
    return {
        "container_id": container_id,
        "sandbox_name": entry["sandbox_name"] if entry else None,
        "owner": username,
        "state": ("consumed" if entry["consumed_at"] is not None else "pending")
        if entry else None,
        "expires_in": START_INTENT_TTL,
    }, 200


@container_bp.post("/register")
def register_runtime_container():
    """Register a container reported by an OCI runtime hook.

    Required JSON fields are ``container_id``, ``host_pid`` and
    ``sandbox_cgroup`` (the sandbox name).  ``container_cgroup`` and
    ``mount_namespace`` are optional observations from the hook and are
    cross-checked when supplied; the Worker never trusts them as identity.

    The state check and the registration itself happen inside one
    ``SbxManager.lifecycle_lock()`` critical section: see
    ``SbxManager.register_runtime_container``.
    """
    body = request.get_json(silent=True) or {}
    container_id = str(body.get("container_id") or "").strip()
    sandbox_value = str(body.get("sandbox_cgroup") or "").strip()
    if not container_id or not sandbox_value:
        return {"error": "container_id 和 sandbox_cgroup 为必填参数"}, 400
    try:
        host_pid = int(body.get("host_pid"))
    except (TypeError, ValueError):
        return {"error": "host_pid 必须为正整数"}, 400
    if host_pid <= 0:
        return {"error": "host_pid 必须为正整数"}, 400

    # 借条优先于 annotation：`neubox docker start` 刚声明过"这个容器该借哪个
    # 沙盒"，而 annotation 里写的是建容器时那个（往往已经 release）。属主对不上
    # 就当没有借条 —— 借条也只能借给容器属主自己名下的沙盒。
    sandbox_name = None
    lent = StartIntentStore.get_instance().take(
        container_id, _sandbox_owner(sandbox_value))
    if lent is not None:
        sandbox_name = _sandbox_name_from_annotation(lent)
        if sandbox_name is None:
            logger.warning(
                "start 借条里的沙盒 '%s' 已经不在了，容器 %s 退回 annotation '%s'",
                lent, container_id, sandbox_value,
            )
    if sandbox_name is None:
        sandbox_name = _sandbox_name_from_annotation(sandbox_value)
    if sandbox_name is None:
        return {
            "error": "sandbox_cgroup 未匹配到 Worker 沙盒",
            "code": "sandbox_not_found",
        }, 404
    if lent is not None and sandbox_name == lent:
        logger.warning(
            "容器 %s 按 start 借条绑到沙盒 '%s'（annotation 指向 '%s'）",
            container_id, sandbox_name, sandbox_value,
        )

    try:
        identity = runtime_container_identity(container_id, host_pid)
    except DockerExecutorError as exc:
        return _runtime_error_response(exc)

    supplied_cgroup = body.get("container_cgroup")
    supplied_mnt = body.get("mount_namespace")
    if supplied_cgroup is not None and str(supplied_cgroup) != identity.container_cgroup:
        return {
            "error": "container_cgroup 与实际值不一致",
            "code": "runtime_identity_changed",
        }, 409
    if supplied_mnt is not None:
        try:
            if int(supplied_mnt) != identity.mount_namespace:
                raise ValueError
        except (TypeError, ValueError):
            return {
                "error": "mount_namespace 与实际值不一致",
                "code": "runtime_identity_changed",
            }, 409

    # 这里不另做孤儿回收：登记落在 ``register_container`` 里，它顺手钉住
    # mnt ns 并对 init host_pid 开 pidfd 挂进 epoll —— 容器一退出，收尸线程
    # 就被事件叫醒并注销；Worker 重启（fd 全丢）或事件处理失败留下的条目由
    # ``reconcile_containers`` 对账清掉。
    manager = SbxManager.get_instance()
    try:
        record, created = manager.register_runtime_container(
            sandbox_name, identity,
        )
    except DockerExecutorError as exc:
        return _runtime_error_response(exc)
    return {
        "sandbox_name": sandbox_name,
        "container_id": record["container_id"],
        "mount_namespace": record["mount_namespace"],
        "container_cgroup": identity.container_cgroup,
        "status": "registered" if created else "already_registered",
    }, 201 if created else 200
