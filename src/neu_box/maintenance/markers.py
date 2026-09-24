"""持久化维护窗口标记。"""

from pathlib import Path

from neu_box.storage import database_path


def pause_marker() -> Path:
    return Path(f"{database_path()}.paused")


def mark_paused() -> Path:
    marker = pause_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(mode=0o600, exist_ok=True)
    try:
        marker.chmod(0o600)
    except OSError:
        pass
    return marker


def clear_pause_marker() -> None:
    pause_marker().unlink(missing_ok=True)
