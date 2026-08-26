from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from deploy import install


FAKE_ROLE = r'''#!/usr/bin/env python3
import os
import sqlite3
import sys
from pathlib import Path

database = Path(os.environ["NEU_BOX_DB_PATH"])
database.parent.mkdir(parents=True, exist_ok=True)
command = sys.argv[-1]
with sqlite3.connect(database) as conn:
    if command == "migrate":
        conn.execute("CREATE TABLE IF NOT EXISTS payload (value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS release_marker (version TEXT)")
        conn.execute("DELETE FROM release_marker")
        version = Path(sys.argv[0]).resolve().parent.parent.name
        conn.execute("INSERT INTO release_marker VALUES (?)", (version,))
        conn.commit()
    elif command == "check":
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    else:
        raise SystemExit(2)
'''


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_checksums(release: Path) -> None:
    checksums = []
    for path in sorted(release.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            checksums.append(
                f"{_sha256(path)}  {path.relative_to(release).as_posix()}"
            )
    (release / "SHA256SUMS").write_text(
        "\n".join(checksums) + "\n", encoding="utf-8"
    )


def _fake_release(tmp_path: Path, version: str) -> Path:
    release = tmp_path / f"source-{version}"
    for role in install.ROLES:
        role_dir = release / role
        role_dir.mkdir(parents=True)
        executable = role_dir / f"neu-box-{role}"
        executable.write_text(FAKE_ROLE, encoding="utf-8")
        executable.chmod(0o755)
    info = release / "share" / "neu-box" / "info" / "gpu_info.sh"
    info.parent.mkdir(parents=True)
    info.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    info.chmod(0o755)
    sandbox = release / "share" / "neu-box" / "sandbox" / "v2"
    sandbox.mkdir(parents=True)
    sandbox_script = sandbox / "sandbox.sh"
    sandbox_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sandbox_script.chmod(0o755)
    (sandbox / "device_block.o").write_bytes(b"fake-bpf-object")
    config = release / "config"
    config.mkdir()
    (config / "worker.env.example").write_text(
        "NEU_BOX_DB_PATH=/var/lib/neu-box/worker/neu_box.db\n"
        "NEU_BOX_PORT=59075\n",
        encoding="utf-8",
    )
    systemd = release / "systemd"
    systemd.mkdir()
    for role in install.ROLES:
        (systemd / f"neu-box-{role}.service").write_text(
            f"[Service]\nExecStart=neu-box-{role}\n", encoding="utf-8"
        )
    installer = release / "neu-box-install"
    installer.write_text("installer", encoding="utf-8")
    installer.chmod(0o755)
    (release / "manifest.json").write_text(json.dumps({
        "format": 1,
        "name": "neu-box",
        "version": version,
        "os": "linux",
        "architecture": install._architecture(),
    }), encoding="utf-8")
    _write_checksums(release)
    return release


def _deploy(
    root: Path,
    source: Path,
    command: str = "install",
) -> int:
    return install.main([
        "--root", str(root),
        "--no-systemd",
        command,
        "--role", "worker",
        "--source", str(source),
        "--no-start",
    ])


def test_staged_install_upgrade_and_database_rollback(tmp_path):
    root = tmp_path / "root"
    release_one = _fake_release(tmp_path, "1.0.0")
    release_two = _fake_release(tmp_path, "1.1.0")

    assert _deploy(root, release_one) == 0
    layout = install.Layout(root)
    worker_db = install._role_database(layout, "worker")
    with sqlite3.connect(worker_db) as conn:
        conn.execute("INSERT INTO payload VALUES ('before-upgrade')")
        conn.commit()
    worker_config_before = install._role_config(layout, "worker").read_bytes()

    assert _deploy(root, release_two, "upgrade") == 0
    assert install._current_release(layout).name == "1.1.0"
    assert install._role_config(layout, "worker").read_bytes() == worker_config_before
    with sqlite3.connect(worker_db) as conn:
        assert conn.execute("SELECT version FROM release_marker").fetchone()[0] == "1.1.0"
        conn.execute("INSERT INTO payload VALUES ('after-upgrade')")
        conn.commit()

    assert install.main([
        "--root", str(root),
        "--no-systemd",
        "rollback",
        "--yes",
        "--no-start",
    ]) == 0

    assert install._current_release(layout).name == "1.0.0"
    with sqlite3.connect(worker_db) as conn:
        values = [row[0] for row in conn.execute("SELECT value FROM payload")]
        marker = conn.execute("SELECT version FROM release_marker").fetchone()[0]
    assert values == ["before-upgrade"]
    assert marker == "1.0.0"


def test_install_self_copies_optional_management_launcher(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    installer = release / "neu-box-install"
    installer.write_text("installer", encoding="utf-8")
    installer.chmod(0o755)
    launcher = release / "run.sh"
    launcher.write_text("launcher", encoding="utf-8")
    launcher.chmod(0o755)
    layout = install.Layout(tmp_path / "root")

    install._install_self(layout, release)

    assert (layout.sbin / "neu-box-install").read_text() == "installer"
    assert (layout.sbin / "neu-box").read_text() == "launcher"
    assert (layout.sbin / "neu-box").stat().st_mode & 0o111


def test_release_checksum_tampering_is_rejected(tmp_path):
    release = _fake_release(tmp_path, "1.0.0")
    (release / "config" / "worker.env.example").write_text(
        "tampered\n", encoding="utf-8"
    )

    try:
        install.verify_release(release)
    except install.InstallError as exc:
        assert "checksum" in str(exc)
    else:
        raise AssertionError("tampered release was accepted")


def test_first_install_imports_legacy_data_without_old_source_paths(tmp_path):
    root = tmp_path / "root"
    release = _fake_release(tmp_path, "1.0.0")
    legacy = tmp_path / "old-worker"
    legacy.mkdir()
    legacy_config = legacy / ".env"
    legacy_config.write_text(
        "listen=10.0.0.8\n"
        "port=60000\n"
        "db_dir=/old/source/worker/db\n"
        "sandbox_script_path=/old/source/worker/scripts/sandbox.sh\n"
        "device_filter=nvidia[0-9]+\n"
        "dev_info_script_path=/old/source/worker/scripts/gpu_info.sh\n",
        encoding="utf-8",
    )
    legacy_database = legacy / "neu_box.db"
    with sqlite3.connect(legacy_database) as conn:
        conn.execute("CREATE TABLE payload (value TEXT)")
        conn.execute("INSERT INTO payload VALUES ('preserved')")
        conn.commit()

    result = install.main([
        "--root", str(root),
        "--no-systemd",
        "install",
        "--role", "worker",
        "--source", str(release),
        "--legacy-config", str(legacy_config),
        "--legacy-database", str(legacy_database),
        "--no-start",
    ])

    assert result == 0
    layout = install.Layout(root)
    installed_config = install._role_config(layout, "worker").read_text()
    assert "NEU_BOX_LISTEN=10.0.0.8" in installed_config
    assert "NEU_BOX_PORT=60000" in installed_config
    assert "NEU_BOX_DEVICE_FILTER=nvidia[0-9]+" in installed_config
    assert "/old/source" not in installed_config
    assert (
        "NEU_BOX_DEVICE_INFO_SCRIPT="
        "/opt/neu-box/current/share/neu-box/info/gpu_info.sh"
    ) in installed_config
    with sqlite3.connect(install._role_database(layout, "worker")) as conn:
        assert conn.execute("SELECT value FROM payload").fetchone()[0] == "preserved"


def test_legacy_import_only_allowed_on_first_install(tmp_path):
    root = tmp_path / "root"
    release = _fake_release(tmp_path, "1.0.0")
    legacy = tmp_path / "old-worker"
    legacy.mkdir()
    legacy_database = legacy / "neu_box.db"
    with sqlite3.connect(legacy_database) as conn:
        conn.execute("CREATE TABLE payload (value TEXT)")
        conn.commit()
    legacy_config = legacy / ".env"
    legacy_config.write_text("port=60000\n", encoding="utf-8")

    assert _deploy(root, release) == 0
    result = install.main([
        "--root", str(root),
        "--no-systemd",
        "install",
        "--role", "worker",
        "--source", str(release),
        "--legacy-config", str(legacy_config),
        "--legacy-database", str(legacy_database),
        "--no-start",
    ])
    assert result == 1


def test_failed_upgrade_restores_program_database_and_state(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    release_one = _fake_release(tmp_path / "one", "1.0.0")
    release_two = _fake_release(tmp_path / "two", "1.1.0")
    assert _deploy(root, release_one) == 0

    layout = install.Layout(root)
    database = install._role_database(layout, "worker")
    state_before = layout.state_file.read_bytes()
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO payload VALUES ('must-survive')")
        conn.commit()

    def fail_migrate(_layout, _release, _roles):
        raise install.InstallError("simulated migration failure")

    monkeypatch.setattr(install, "_migrate_live", fail_migrate)
    assert _deploy(root, release_two, "upgrade") == 1

    assert install._current_release(layout).name == "1.0.0"
    assert layout.state_file.read_bytes() == state_before
    with sqlite3.connect(database) as conn:
        marker = conn.execute("SELECT version FROM release_marker").fetchone()[0]
        payload = conn.execute("SELECT value FROM payload").fetchall()
    assert marker == "1.0.0"
    assert payload == [("must-survive",)]


def test_failed_first_install_leaves_clean_state(tmp_path, monkeypatch):
    root = tmp_path / "root"
    release = _fake_release(tmp_path, "1.0.0")
    layout = install.Layout(root)

    def fail_migrate(_layout, _release, _roles):
        raise install.InstallError("simulated migration failure")

    monkeypatch.setattr(install, "_migrate_live", fail_migrate)
    assert _deploy(root, release) == 1

    assert not layout.current.exists()
    assert not layout.current.is_symlink()
    assert not layout.state_file.exists()


def test_same_version_with_different_contents_is_rejected(tmp_path):
    root = tmp_path / "root"
    release_one = _fake_release(tmp_path / "one", "1.0.0")
    release_two = _fake_release(tmp_path / "two", "1.0.0")
    worker_template = release_two / "config" / "worker.env.example"
    worker_template.write_text(
        worker_template.read_text(encoding="utf-8") + "CHANGED=yes\n",
        encoding="utf-8",
    )
    _write_checksums(release_two)

    assert _deploy(root, release_one) == 0
    assert _deploy(root, release_two, "upgrade") == 1
    assert install._current_release(install.Layout(root)).name == "1.0.0"


def test_install_and_upgrade_commands_have_distinct_lifecycle(tmp_path):
    release_one = _fake_release(tmp_path / "one", "1.0.0")
    release_two = _fake_release(tmp_path / "two", "1.1.0")
    empty_root = tmp_path / "empty-root"
    installed_root = tmp_path / "installed-root"

    assert _deploy(empty_root, release_two, "upgrade") == 1
    assert _deploy(installed_root, release_one) == 0
    assert _deploy(installed_root, release_two) == 1
    assert install._current_release(install.Layout(installed_root)).name == "1.0.0"


def test_staged_absolute_path_cannot_escape_root(tmp_path):
    layout = install.Layout(tmp_path / "root")
    try:
        install._mapped_absolute(layout, "/../../outside.db")
    except ValueError as exc:
        assert "escapes installation root" in str(exc)
    else:
        raise AssertionError("path escaped the staged installation root")


def test_restore_selinux_contexts_uses_target_policy(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    calls = []

    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: "/usr/sbin/restorecon" if command == "restorecon" else None,
    )
    monkeypatch.setattr(
        install,
        "_run",
        lambda command, **_kwargs: calls.append(command),
    )

    install._restore_selinux_contexts(
        install.Layout(Path("/")),
        [release, tmp_path / "missing"],
        recursive=True,
    )

    assert calls == [["/usr/sbin/restorecon", "-RF", str(release)]]


def test_staged_install_does_not_relabel_host_paths(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(shutil, "which", lambda _command: "/usr/sbin/restorecon")
    monkeypatch.setattr(
        install,
        "_run",
        lambda command, **_kwargs: calls.append(command),
    )

    install._restore_selinux_contexts(
        install.Layout(tmp_path / "root"),
        [tmp_path],
        recursive=True,
    )

    assert calls == []


def test_enforcing_selinux_requires_restorecon(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _command: None)
    monkeypatch.setattr(install, "_selinux_enforcing", lambda: True)

    try:
        install._restore_selinux_contexts(
            install.Layout(Path("/")),
            [tmp_path],
        )
    except install.InstallError as exc:
        assert "restorecon" in str(exc)
    else:
        raise AssertionError("enforcing SELinux without restorecon was accepted")


def test_atomic_rewrite_preserves_existing_owner(tmp_path, monkeypatch):
    target = tmp_path / "worker.env"
    target.write_text("before\n", encoding="utf-8")
    metadata = target.stat()
    chowns = []
    monkeypatch.setattr(
        install.os,
        "chown",
        lambda path, uid, gid: chowns.append((Path(path), uid, gid)),
    )

    install._atomic_write(target, "after\n", 0o640)

    assert target.read_text(encoding="utf-8") == "after\n"
    assert chowns == [(
        target.with_name(f".{target.name}.tmp-{install.os.getpid()}"),
        metadata.st_uid,
        metadata.st_gid,
    )]
