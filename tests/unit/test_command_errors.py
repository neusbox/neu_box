from __future__ import annotations

from flask import Flask
import pytest

from neu_box.worker.executor import command as command_module


class _FakeSandboxManager:
    def join_sandbox(self, _name: str, _pid: int) -> bool:
        return True


def _command_client() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(command_module.command_bp, url_prefix="/command")
    return app


def test_submit_rejects_unknown_system_user():
    client = _command_client().test_client()
    username = "__neu_box_missing_user__"

    response = client.post("/command/run", json={
        "user_id": username,
        "command": "true",
    })

    assert response.status_code == 400
    assert response.json == {"error": f"系统用户 {username} 不存在"}


@pytest.mark.parametrize(
    ("task_id", "shell_command", "returncode", "message"),
    [
        ("syntax", "if", 2, "syntax error"),
        (
            "missing_command",
            "__neu_box_command_that_does_not_exist__",
            127,
            "command not found",
        ),
        ("stderr", "printf 'visible stderr\\n' >&2; exit 7", 7, "visible stderr"),
    ],
)
def test_host_shell_errors_are_returned_in_task_log(
    tmp_path,
    monkeypatch,
    task_id,
    shell_command,
    returncode,
    message,
):
    log_dir = tmp_path / "logs"
    cgroup_procs = tmp_path / "cgroup.procs"
    cgroup_procs.touch()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("", encoding="utf-8")

    sandbox = _FakeSandboxManager()
    monkeypatch.setattr(command_module, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(command_module, "_cgroup_procs_path", lambda _name: str(cgroup_procs))
    monkeypatch.setattr(
        command_module.SbxManager,
        "get_instance",
        classmethod(lambda _cls: sandbox),
    )
    monkeypatch.setenv("HOME", str(home))

    result = command_module.execute_in_sandbox(
        command=shell_command,
        sandbox_name=f"sbx_test_{task_id}.slice",
        timeout=5,
    )

    assert result["returncode"] == returncode
    assert message in result["stdout"]

    response = _command_client().test_client().get(
        f"/command/result/{task_id}/log?raw=1"
    )
    assert response.status_code == 200
    assert message in response.get_data(as_text=True)
