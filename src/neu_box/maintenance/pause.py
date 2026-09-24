"""暂停 Worker、等待任务结束，并执行备份、清理和停服。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import urllib.request

from neu_box.config import env_text, user_data_dir
from neu_box.migrations.engine import backup_database
from neu_box.storage import database_path
from neu_box.maintenance.markers import (
    pause_marker, mark_paused, clear_pause_marker,
)
from neu_box.config import sandbox_executable_path


SERVICE = "neuboxd.service"



def request_worker(port: int, path: str, method: str = "GET", timeout: float = 5) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def control_worker(command: str, port: int) -> dict:
    payload = request_worker(port, f"/maintenance/{command}", "POST")
    print(payload['message'], flush=True)
    return payload['maintenance']


def require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("维护操作需要 root")
    os.umask(0o077)


def stop_worker_after_cleanup() -> None:
    """Ask systemd's main process to exit without creating a stop job.

    The unit rejects manual stop/restart jobs; pause is the explicit,
    already-quiet maintenance path and therefore signals the service PID
    directly.  systemd observes the normal SIGTERM exit and does not treat
    this as an operator stop request.
    """
    result = subprocess.run(
        ["systemctl", "show", "--property=MainPID", "--value", SERVICE],
        check=True, capture_output=True, text=True,
    )
    try:
        pid = int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("无法取得 Worker MainPID") from exc
    if pid <= 1 or pid == os.getpid():
        raise RuntimeError(f"Worker MainPID 无效: {pid}")
    os.kill(pid, signal.SIGTERM)


def pause(port: int, timeout: int, config: Path | None) -> None:
    require_root()
    deadline = time.monotonic() + timeout if timeout else float('inf')
    # Arm the durable gate before asking the service to pause.  This closes
    # the crash window between a successful HTTP pause and marker creation;
    # if the request itself fails, a restart still comes up paused and the
    # operator can retry or remove the marker through setup/resume.
    mark_paused()
    status = control_worker("pause", port)
    print("等待运行任务结束和沙盒回收；pending 保留，不自动中断任务。", flush=True)
    while not status['quiet']:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "等待维护超时，Worker 保持暂停，尚未停服或 cleanup。"
                "维护期间 resume 会被拒绝；请重试 pause，"
                "或重启服务后再 resume。"
                f"当前状态: {json.dumps(status, ensure_ascii=False)}"
            )
        status = request_worker(port, "/maintenance", timeout=min(5, remaining))['maintenance']
        # 不需要处理"期间被 resume"：维护进行中 resume 会被 Worker 拒绝
        # （api/maintenance.py），paused 只可能保持为真。
        if not status['quiet']:
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    backup_dir = env_text("NEU_BOX_BACKUP_DIR") or user_data_dir("worker").parent / "backups"
    backup = backup_database(database_path(), backup_dir, "worker")
    print(f"数据库备份: {backup}", flush=True)
    if config is not None:
        config_backup = backup.with_suffix(".env")
        shutil.copyfile(config, config_backup)
        print(f"配置备份: {config_backup}", flush=True)
    subprocess.run([str(sandbox_executable_path()), "cleanup"], check=True)
    # 备份和 cleanup 全部成功后才停服；前面失败时保留暂停中的 API 供重试。
    stop_worker_after_cleanup()
    print("Worker 已停止，旧 BPF 已清理，可以安装新版 RPM。", flush=True)
