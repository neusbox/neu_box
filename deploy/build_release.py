#!/usr/bin/env python3
"""Build the native sandbox, PyInstaller Worker bundle, and binary RPM."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "release"


def _run(command: list[str], *, cwd: Path = ROOT) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def _build_native(build_dir: Path, *, static_libbpf: bool) -> None:
    shutil.rmtree(build_dir, ignore_errors=True)
    _run([
        "cmake",
        "-S", str(ROOT / "native" / "sandbox"),
        "-B", str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DNEU_BOX_SANDBOX_STATIC_LIBBPF=" + (
            "ON" if static_libbpf else "OFF"
        ),
    ])
    _run([
        "cmake", "--build", str(build_dir),
        "--config", "Release",
        "--parallel",
    ])
    _run([
        "ctest",
        "--test-dir", str(build_dir),
        "--build-config", "Release",
        "--output-on-failure",
    ])


def _build_worker(dist_dir: Path, work_dir: Path) -> None:
    shutil.rmtree(dist_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    _run([
        sys.executable,
        "-m", "PyInstaller",
        "--log-level=WARN",
        "--clean",
        "--noconfirm",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        str(ROOT / "deploy" / "pyinstaller" / "worker.spec"),
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(ROOT / "dist" / "rpm"))
    parser.add_argument("--release", default="1", help="RPM release number")
    parser.add_argument(
        "--dynamic-libbpf",
        action="store_true",
        help=(
            "development build linked to the host libbpf; production RPMs "
            "should keep the default statically linked libbpf"
        ),
    )
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="compose the binary payload source archive without rpmbuild",
    )
    args = parser.parse_args()

    native_build = BUILD_ROOT / "native-sandbox"
    pyinstaller_dist = BUILD_ROOT / "pyinstaller-dist"
    pyinstaller_work = BUILD_ROOT / "pyinstaller-work" / "worker"

    rpm_release = args.release
    if args.dynamic_libbpf and not rpm_release.endswith(".dev"):
        rpm_release += ".dev"

    _build_native(
        native_build,
        static_libbpf=not args.dynamic_libbpf,
    )
    _build_worker(pyinstaller_dist, pyinstaller_work)

    command = [
        sys.executable,
        str(ROOT / "deploy" / "rpm" / "build_rpm.py"),
        "--release", rpm_release,
        "--output-dir", str(Path(args.output_dir).expanduser().resolve()),
        "--worker-bundle", str(pyinstaller_dist / "neu-box-worker"),
        "--sandbox-executable", str(native_build / "neu-box-sandbox"),
        "--bpf-object", str(native_build / "device_block.o"),
    ]
    if args.source_only:
        command.append("--source-only")
    if args.dynamic_libbpf:
        command.append("--allow-dynamic-libbpf")
    _run(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
