#!/usr/bin/env python3
"""Build the native sandbox, PyInstaller bundles, and the binary RPM."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "release"


def _run(command: list[str], *, cwd: Path = ROOT) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def _require_pidfd_open() -> None:
    """拒绝用一个没有 os.pidfd_open 的解释器打包。

    这个函数是 CPython 编译期决定的（构建时 <sys/syscall.h> 里要有
    SYS_pidfd_open），conda 拿老 sysroot 编的解释器就没有。PyInstaller 会把
    构建解释器的运行时一起打包，所以在缺它的解释器上打出来的 Worker，会在
    ``/container/register`` 里抛 AttributeError、回 500 —— 装完一个容器都登记
    不了，而单元测试全把这条路径换成了替身，构建期不拦就没人拦得住。
    """
    if not hasattr(os, "pidfd_open"):
        raise SystemExit(
            f"构建解释器 {sys.executable} 没有 os.pidfd_open，打出来的 Worker "
            f"会登记不了容器。换一个有它的解释器（pyproject.toml 的 "
            f"requires-python 上界就是为此设的）。"
        )


def _build_native(build_dir: Path) -> None:
    shutil.rmtree(build_dir, ignore_errors=True)
    _run([
        "make",
        "-C", str(ROOT / "native" / "sandbox"),
        f"BUILD_DIR={build_dir}",
        "all",
        "test",
    ])


def _build_worker(dist_dir: Path, work_dir: Path) -> None:
    # 多个 PyInstaller 产物共用同一个 --distpath，所以只清自己的目录。
    shutil.rmtree(dist_dir / "neuboxd", ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    _run([
        sys.executable,
        "-m", "PyInstaller",
        "--log-level=WARN",
        "--clean",
        "--noconfirm",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        str(ROOT / "deploy" / "pyinstaller" / "neuboxd.spec"),
    ])


def _build_ctl(dist_dir: Path, work_dir: Path) -> None:
    # ``neuboxd`` 是 daemon，管理命令单独打 ``neuboxctl``，避免 daemon
    # 暴露 setup/pause/resume/db。
    shutil.rmtree(dist_dir / "neuboxctl", ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    _run([
        sys.executable,
        "-m", "PyInstaller",
        "--log-level=WARN",
        "--clean",
        "--noconfirm",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        str(ROOT / "deploy" / "pyinstaller" / "neuboxctl.spec"),
    ])
    entry = dist_dir / "neuboxctl" / "neuboxctl"
    _run([str(entry), "help"])


def _build_deployment_tests(dist_dir: Path, work_dir: Path) -> None:
    """打包第 3 层实机验收套件。

    产物自带 pytest 和套件源码，部署机不需要 Python —— 装完 worker RPM 就能
    ``neuboxctl test``。
    """
    shutil.rmtree(dist_dir / "neu-box-deployment-tests", ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    _run([
        sys.executable,
        "-m", "PyInstaller",
        "--log-level=WARN",
        "--clean",
        "--noconfirm",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        str(ROOT / "deploy" / "pyinstaller" / "deployment_tests.spec"),
    ])
    # 打包期自检：产物能起来、pytest 真的在包里、套件源码齐全。
    entry = dist_dir / "neu-box-deployment-tests" / "neu-box-deployment-tests"
    _run([str(entry), "--self-check"])


def main() -> int:
    _require_pidfd_open()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(ROOT / "dist" / "rpm"))
    parser.add_argument("--release", default="1", help="RPM release number")
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="compose the binary payload source archive without rpmbuild",
    )
    args = parser.parse_args()

    native_build = BUILD_ROOT / "native-sandbox"
    pyinstaller_dist = BUILD_ROOT / "pyinstaller-dist"
    pyinstaller_work = BUILD_ROOT / "pyinstaller-work"

    _build_native(native_build)
    _build_worker(pyinstaller_dist, pyinstaller_work / "worker")
    _build_ctl(pyinstaller_dist, pyinstaller_work / "ctl")
    _build_deployment_tests(
        pyinstaller_dist, pyinstaller_work / "deployment-tests",
    )

    command = [
        sys.executable,
        str(ROOT / "deploy" / "rpm" / "build_rpm.py"),
        "--release", args.release,
        "--output-dir", str(Path(args.output_dir).expanduser().resolve()),
        "--worker-bundle", str(pyinstaller_dist / "neuboxd"),
        "--ctl-bundle", str(pyinstaller_dist / "neuboxctl"),
        "--tests-bundle", str(pyinstaller_dist / "neu-box-deployment-tests"),
        "--sandbox-executable", str(native_build / "neu-box-sandbox"),
        "--bpf-object", str(native_build / "device_block.o"),
    ]
    if args.source_only:
        command.append("--source-only")
    _run(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
