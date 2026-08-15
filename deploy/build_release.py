#!/usr/bin/env python3
"""Build a versioned, checksummed Neu Box deployment archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _version() -> str:
    namespace: dict[str, str] = {}
    exec((ROOT / "src" / "neu_box" / "__init__.py").read_text(), namespace)
    return namespace["__version__"]


def _architecture() -> str:
    machine = platform.machine().lower()
    aliases = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    try:
        return aliases[machine]
    except KeyError as exc:
        raise SystemExit(f"Unsupported build architecture: {machine}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, env=env)


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(ROOT / "dist"))
    parser.add_argument("--skip-pyinstaller", action="store_true")
    args = parser.parse_args()

    version = _version()
    if not version or "/" in version or version in {".", ".."}:
        parser.error("project version must be a non-empty path-safe value")
    architecture = _architecture()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    work_root = ROOT / "build" / "release"
    pyi_dist = work_root / "pyinstaller-dist"
    pyi_work = work_root / "pyinstaller-work"
    generated = work_root / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    go = shutil.which("go")
    if not go:
        raise SystemExit(
            "missing Go toolchain: install Go to build the static neu-sbox client"
        )
    bpf_object = generated / "device_block.o"
    _run([
        "clang", "-O2", "-g", "-target", "bpf", "-c",
        str(
            ROOT
            / "src"
            / "neu_box"
            / "worker"
            / "resources"
            / "sandbox"
            / "v2"
            / "device_block.bpf.c"
        ),
        "-o", str(bpf_object),
    ])

    go_client = generated / "neu-sbox"
    go_environment = os.environ.copy()
    go_environment.update({
        "CGO_ENABLED": "0",
        "GOOS": "linux",
        "GOARCH": architecture,
        "GOTOOLCHAIN": "local",
        "GOCACHE": str(work_root / "go-cache"),
        "GOMODCACHE": str(work_root / "go-mod-cache"),
    })
    _run([
        go,
        "build",
        "-trimpath",
        "-buildvcs=false",
        "-tags=netgo,osusergo",
        "-ldflags",
        f"-s -w -X main.version={version}",
        "-o",
        str(go_client),
        ".",
    ], env=go_environment, cwd=ROOT / "client" / "neu-sbox")
    if not args.skip_pyinstaller:
        shutil.rmtree(pyi_dist, ignore_errors=True)
        shutil.rmtree(pyi_work, ignore_errors=True)
        build_environment = os.environ.copy()
        build_environment["NEU_BOX_BUILD_BPF_OBJECT"] = str(bpf_object)
        for role in ("master", "worker", "installer"):
            _run([
                sys.executable,
                "-m",
                "PyInstaller",
                "--log-level=WARN",
                "--clean",
                "--noconfirm",
                "--distpath",
                str(pyi_dist),
                "--workpath",
                str(pyi_work / role),
                str(ROOT / "deploy" / "pyinstaller" / f"{role}.spec"),
            ], env=build_environment)

    for role in ("master", "worker"):
        if not (pyi_dist / f"neu-box-{role}").is_dir():
            raise SystemExit(f"missing PyInstaller output for {role}")
    if not (pyi_dist / "neu-box-install").is_file():
        raise SystemExit("missing PyInstaller output for installer")

    archive_name = f"neu-box-{version}-linux-{architecture}"
    with tempfile.TemporaryDirectory(prefix="neu-box-release-") as raw_temp:
        staging = Path(raw_temp) / archive_name
        staging.mkdir()
        _copy_tree(pyi_dist / "neu-box-master", staging / "master")
        _copy_tree(pyi_dist / "neu-box-worker", staging / "worker")
        shutil.copy2(pyi_dist / "neu-box-install", staging / "neu-box-install")
        os.chmod(staging / "neu-box-install", 0o755)
        _copy_tree(ROOT / "deploy" / "config", staging / "config")
        _copy_tree(ROOT / "deploy" / "systemd", staging / "systemd")
        _copy_tree(
            ROOT / "src" / "neu_box" / "worker" / "resources",
            staging / "share" / "neu-box",
        )
        client_destination = (
            staging / "share" / "neu-box" / "client" / "neu-sbox"
        )
        client_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(go_client, client_destination)
        os.chmod(client_destination, 0o755)
        bpf_source = staging / "share" / "neu-box" / "sandbox" / "v2" / "device_block.bpf.c"
        bpf_object = bpf_source.with_suffix("").with_suffix(".o")
        shutil.copy2(generated / "device_block.o", bpf_object)
        shutil.copy2(ROOT / "LICENSE", staging / "LICENSE")
        shutil.copy2(ROOT / "README.md", staging / "README.md")
        _copy_tree(ROOT / "docs", staging / "docs")

        manifest = {
            "format": 1,
            "name": "neu-box",
            "version": version,
            "os": "linux",
            "architecture": architecture,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        checksums: list[str] = []
        for path in sorted(staging.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                relative = path.relative_to(staging)
                checksums.append(f"{_sha256(path)}  {relative.as_posix()}")
        (staging / "SHA256SUMS").write_text(
            "\n".join(checksums) + "\n",
            encoding="utf-8",
        )

        archive = output_dir / f"{archive_name}.tar.gz"
        temporary_archive = archive.with_name(archive.name + ".tmp")
        with tarfile.open(temporary_archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            tar.add(staging, arcname=archive_name)
        os.replace(temporary_archive, archive)
        checksum_file = archive.with_suffix(archive.suffix + ".sha256")
        checksum_file.write_text(
            f"{_sha256(archive)}  {archive.name}\n",
            encoding="utf-8",
        )
        print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
