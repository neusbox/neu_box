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

from flask import Blueprint, request

from neu_box.runtime.containers import (
    DockerExecutorError,
    runtime_container_identity,
)
from neu_box.runtime.sandbox import SbxManager

container_bp = Blueprint("container", __name__)


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

    sandbox_name = _sandbox_name_from_annotation(sandbox_value)
    if sandbox_name is None:
        return {
            "error": "sandbox_cgroup 未匹配到 Worker 沙盒",
            "code": "sandbox_not_found",
        }, 404

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
