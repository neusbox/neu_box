from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"


def _architecture() -> str:
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }[platform.machine().lower()]


def _fake_installed_installer(tmp_path: Path, version: str) -> Path:
    installer = tmp_path / "installed-neu-box-install"
    installer.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == status ]]; then\n"
        f"  printf '%s\\n' '{{\"current_version\": \"{version}\"}}'\n"
        "else\n"
        "  printf '%s\\n' \"$@\" >\"$NEU_BOX_UPDATE_RECORD\"\n"
        "  exit \"${NEU_BOX_FAKE_UPGRADE_EXIT:-0}\"\n"
        "fi\n",
        encoding="utf-8",
    )
    installer.chmod(0o755)
    return installer


def _release_assets(
    tmp_path: Path,
    version: str,
    *,
    archive_root: str | None = None,
) -> tuple[str, dict[str, bytes]]:
    architecture = _architecture()
    expected_root = f"neu-box-{version}-linux-{architecture}"
    root_name = archive_root or expected_root
    release = tmp_path / "release"
    release.mkdir()
    (release / "manifest.json").write_text(
        '{"format":1,"name":"neu-box"}\n', encoding="utf-8"
    )
    installer = release / "neu-box-install"
    installer.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'downloaded installer must not run directly' >&2\n"
        "exit 99\n",
        encoding="utf-8",
    )
    installer.chmod(0o755)

    asset_name = f"neu-box-{version}-linux-{architecture}.tar.gz"
    archive = tmp_path / asset_name
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(release, arcname=root_name)
    archive_bytes = archive.read_bytes()
    checksum = hashlib.sha256(archive_bytes).hexdigest()
    return f"v{version}", {
        asset_name: archive_bytes,
        f"{asset_name}.sha256": (
            f"{checksum}  {asset_name}\n".encode()
        ),
    }


def _fake_curl(
    tmp_path: Path,
    tag: str,
    assets: dict[str, bytes],
) -> tuple[Path, Path, Path]:
    asset_dir = tmp_path / "release-assets"
    asset_dir.mkdir()
    for name, payload in assets.items():
        (asset_dir / name).write_bytes(payload)

    request_log = tmp_path / "curl-requests"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
head_request=0
output=''
url=''
while (($#)); do
    case "$1" in
        --head) head_request=1; shift ;;
        --output) output="$2"; shift 2 ;;
        --write-out|--connect-timeout|--max-time|--retry|--retry-delay|--proto|--proto-redir)
            shift 2
            ;;
        --fail|--silent|--show-error|--location) shift ;;
        *) url="$1"; shift ;;
    esac
done
if ((head_request)); then
    printf 'HEAD %s\\n' "$url" >>"$NEU_BOX_FAKE_REQUEST_LOG"
    printf '%s/%s/releases/tag/%s' \
        "${NEU_BOX_RELEASE_BASE_URL%/}" \
        "$NEU_BOX_RELEASE_REPOSITORY" \
        "$NEU_BOX_FAKE_RELEASE_TAG"
else
    printf 'GET %s\\n' "$url" >>"$NEU_BOX_FAKE_REQUEST_LOG"
    name="${url##*/}"
    cp "$NEU_BOX_FAKE_ASSET_DIR/$name" "$output"
fi
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return fake_bin, asset_dir, request_log


def _run_online_update(
    tmp_path: Path,
    installed_version: str,
    tag: str,
    assets: dict[str, bytes],
    *arguments: str,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    installed = _fake_installed_installer(tmp_path, installed_version)
    fake_bin, asset_dir, request_log = _fake_curl(tmp_path, tag, assets)
    record = tmp_path / "upgrade-arguments"
    command = """
source "$1"
INSTALLED_INSTALLER="$2"
as_root() { "$@"; }
shift 2
online_update "$@"
"""
    environment = os.environ.copy()
    environment.update({
        "NEU_BOX_RELEASE_BASE_URL": "http://release.test",
        "NEU_BOX_RELEASE_REPOSITORY": "neusbox/neu_box",
        "NEU_BOX_CURRENT_MANIFEST": str(tmp_path / "missing-current-manifest.json"),
        "NEU_BOX_UPDATE_RECORD": str(record),
        "NEU_BOX_FAKE_RELEASE_TAG": tag,
        "NEU_BOX_FAKE_ASSET_DIR": str(asset_dir),
        "NEU_BOX_FAKE_REQUEST_LOG": str(request_log),
        "TMPDIR": str(tmp_path),
        "PATH": f"{fake_bin}:{environment['PATH']}",
    })
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(RUN_SH), str(installed), *arguments],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )
    requests = (
        request_log.read_text(encoding="utf-8").splitlines()
        if request_log.exists()
        else []
    )
    return result, record, requests


def test_installed_version_reads_public_current_manifest_without_status(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"version": "2.3.4"}\n', encoding="utf-8")
    environment = os.environ.copy()
    environment["NEU_BOX_CURRENT_MANIFEST"] = str(manifest)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; installed_version /definitely/missing/installer',
            "bash",
            str(RUN_SH),
        ],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "2.3.4\n"


def test_online_update_downloads_verifies_extracts_and_runs_new_installer(tmp_path):
    tag, assets = _release_assets(tmp_path, "1.1.0")
    result, record, requests = _run_online_update(
        tmp_path, "1.0.0", tag, assets, "--yes"
    )

    assert result.returncode == 0, result.stderr
    arguments = record.read_text(encoding="utf-8").splitlines()
    assert arguments[:3] == ["upgrade", "--role", "worker"]
    assert arguments[3] == "--source"
    source = Path(arguments[4])
    assert source.name == f"neu-box-1.1.0-linux-{_architecture()}"
    assert not source.exists(), "在线更新临时目录没有被清理"
    assert "SHA256 校验通过" in result.stdout
    assert "在线更新完成: 1.0.0 -> 1.1.0" in result.stdout
    assert "HEAD http://release.test/neusbox/neu_box/releases/latest" in requests
    assert any(line.startswith("GET ") and line.endswith(".tar.gz") for line in requests)


def test_check_update_same_version_does_not_download_assets(tmp_path):
    tag, assets = _release_assets(tmp_path, "1.1.0")
    result, record, requests = _run_online_update(
        tmp_path, "1.1.0", tag, assets, "--check-only"
    )

    assert result.returncode == 0, result.stderr
    assert "无需更新" in result.stdout
    assert not record.exists()
    assert not any(line.startswith("GET ") for line in requests)


def test_online_update_rejects_corrupt_checksum(tmp_path):
    tag, assets = _release_assets(tmp_path, "1.1.0")
    checksum_name = next(name for name in assets if name.endswith(".sha256"))
    asset_name = checksum_name.removesuffix(".sha256")
    assets[checksum_name] = f"{'0' * 64}  {asset_name}\n".encode()

    result, record, _requests = _run_online_update(
        tmp_path, "1.0.0", tag, assets, "--yes"
    )

    assert result.returncode != 0
    assert "SHA256 校验失败" in result.stderr
    assert not record.exists()
    assert not list(tmp_path.glob("neu-box-update.*"))


def test_online_update_rejects_unexpected_archive_root(tmp_path):
    tag, assets = _release_assets(
        tmp_path, "1.1.0", archive_root="unexpected-root"
    )
    result, record, _requests = _run_online_update(
        tmp_path, "1.0.0", tag, assets, "--yes"
    )

    assert result.returncode != 0
    assert "非预期顶层路径" in result.stderr
    assert not record.exists()


def test_online_update_rejects_downgrade_without_force(tmp_path):
    tag, assets = _release_assets(tmp_path, "1.0.0")
    result, record, requests = _run_online_update(
        tmp_path, "1.1.0", tag, assets, "--yes"
    )

    assert result.returncode != 0
    assert "旧于当前版本" in result.stderr
    assert not record.exists()
    assert not any(line.startswith("GET ") for line in requests)


def test_online_update_propagates_installer_failure_and_cleans_temp(
    tmp_path,
    monkeypatch,
):
    tag, assets = _release_assets(tmp_path, "1.1.0")
    monkeypatch.setenv("NEU_BOX_FAKE_UPGRADE_EXIT", "42")
    result, record, _requests = _run_online_update(
        tmp_path, "1.0.0", tag, assets, "--yes"
    )

    assert result.returncode == 42
    assert record.exists()
    assert not list(tmp_path.glob("neu-box-update.*"))
    assert "在线更新完成" not in result.stdout
