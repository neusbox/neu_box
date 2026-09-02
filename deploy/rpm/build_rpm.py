#!/usr/bin/env python3
"""Compose and build a Neu Box Worker RPM from prebuilt release artifacts."""

from __future__ import annotations

import argparse
import gzip
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RPM_DIR = Path(__file__).resolve().parent
DEFAULT_BUILD = ROOT / "build" / "release"


def _project_version() -> str:
    text = (ROOT / "src" / "neu_box" / "__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("cannot read __version__ from src/neu_box/__init__.py")
    return match.group(1)


def _validate_bundle_symlink(root: Path, path: Path, target: str) -> Path:
    if Path(target).is_absolute():
        raise SystemExit(f"Worker bundle contains an absolute symlink: {path}")
    try:
        resolved = (path.parent / target).resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(
            f"Worker bundle symlink is broken or escapes the bundle: "
            f"{path} -> {target}"
        ) from exc
    return resolved


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _require_file(path: Path, description: str, *, executable: bool = False) -> None:
    if not path.is_file():
        raise SystemExit(f"missing {description}: {path}")
    if executable and not os.access(path, os.X_OK):
        raise SystemExit(f"{description} is not executable: {path}")


def _require_dir(path: Path, description: str) -> None:
    if not path.is_dir():
        raise SystemExit(f"missing {description}: {path}")


def _copy_file(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(mode)


def _copy_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)


def _publish_file(source: Path, destination: Path) -> None:
    """Atomically publish a release file, replacing the same output name."""
    if destination.is_symlink() or (
        destination.exists() and not destination.is_file()
    ):
        raise SystemExit(
            f"release output target is not a regular file: {destination}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(destination)


def _run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _capture(command: list[str], description: str, *, timeout: int = 15) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"missing build command: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"timed out while {description}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise SystemExit(f"failed while {description}{suffix}") from exc
    return result.stdout


def _dynamic_dependencies(path: Path) -> str:
    return _capture(
        ["readelf", "--dynamic", str(path)],
        f"reading native sandbox ELF: {path}",
    )


def _dynamic_libbpf_dependencies(dynamic_section: str) -> list[str]:
    sonames = re.findall(
        r"Shared library:\s*\[([^]]+)\]",
        dynamic_section,
    )
    return sorted({
        soname for soname in sonames
        if re.match(r"^libbpf\.so(?:\.|$)", soname)
    })


def _elf_headers(path: Path, description: str) -> dict[str, str]:
    output = _capture(
        ["readelf", "--file-header", str(path)],
        f"reading {description} ELF header: {path}",
    )
    return dict(
        re.findall(
            r"^\s*(Class|Data|Type|Machine):\s*(.*?)\s*$",
            output,
            flags=re.MULTILINE,
        )
    )


def _expected_elf_machine() -> str:
    return {
        "aarch64": "AArch64",
        "x86_64": "Advanced Micro Devices X86-64",
    }[platform.machine()]


def _validate_native_sandbox(path: Path) -> None:
    headers = _elf_headers(path, "native sandbox")
    expected = {
        "Class": "ELF64",
        "Data": "2's complement, little endian",
        "Machine": _expected_elf_machine(),
    }
    for field, value in expected.items():
        if headers.get(field) != value:
            raise SystemExit(
                f"native sandbox has unexpected {field}: "
                f"{headers.get(field)!r} (expected {value!r})"
            )
    if not headers.get("Type", "").startswith(("EXEC ", "DYN ")):
        raise SystemExit(
            "native sandbox is not an executable ELF: "
            f"{headers.get('Type')!r}"
        )

    help_output = _capture(
        [str(path), "--help"],
        f"executing native sandbox self-check: {path}",
    )
    required_help = (
        "--bpf-object PATH",
        "create <name> <cpu> <mem>",
        "join <name> <PID>",
        "cleanup",
    )
    if any(marker not in help_output for marker in required_help):
        raise SystemExit(
            "native sandbox --help output does not identify the expected CLI"
        )


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError as exc:
        raise SystemExit(f"cannot read Worker bundle file: {path}: {exc}") from exc


def _validate_worker_elf_architecture(
    worker_bundle: Path,
    worker: Path,
) -> None:
    if worker_bundle.is_symlink():
        raise SystemExit(
            f"PyInstaller Worker bundle must not be a symlink: {worker_bundle}"
        )
    bundle_root = worker_bundle.resolve()
    if worker.is_symlink():
        _validate_bundle_symlink(bundle_root, worker, os.readlink(worker))
    if not _is_elf(worker):
        raise SystemExit(
            "PyInstaller Worker entry point is not an ELF executable: "
            f"{worker}"
        )

    expected = {
        "Class": "ELF64",
        "Data": "2's complement, little endian",
        "Machine": _expected_elf_machine(),
    }
    for current, directory_names, file_names in os.walk(
        worker_bundle,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        for directory_name in directory_names:
            directory = Path(current) / directory_name
            if directory.is_symlink():
                _validate_bundle_symlink(
                    bundle_root,
                    directory,
                    os.readlink(directory),
                )
        for file_name in sorted(file_names):
            path = Path(current) / file_name
            if path.is_symlink():
                _validate_bundle_symlink(
                    bundle_root,
                    path,
                    os.readlink(path),
                )
                continue
            if not path.is_file() or not _is_elf(path):
                continue
            relative = path.relative_to(worker_bundle)
            headers = _elf_headers(path, f"Worker bundle file {relative}")
            if headers.get("Machine") == "Linux BPF":
                continue
            for field, expected_value in expected.items():
                if headers.get(field) != expected_value:
                    raise SystemExit(
                        f"Worker bundle ELF {relative} has unexpected {field}: "
                        f"{headers.get(field)!r} (expected {expected_value!r})"
                    )


def _validate_worker_version(worker: Path, expected: str) -> None:
    observed = _capture(
        [str(worker), "--version"],
        f"checking PyInstaller Worker version: {worker}",
    ).strip()
    if observed != expected:
        raise SystemExit(
            "PyInstaller Worker version does not match RPM Version: "
            f"worker={observed!r}, rpm={expected!r}"
        )


def _validate_bpf_object(path: Path) -> None:
    observed_headers = _elf_headers(path, "BPF")
    expected_headers = {
        "Class": "ELF64",
        "Data": "2's complement, little endian",
        "Type": "REL (Relocatable file)",
        "Machine": "Linux BPF",
    }
    for field, expected in expected_headers.items():
        if observed_headers.get(field) != expected:
            raise SystemExit(
                f"BPF object has unexpected {field}: "
                f"{observed_headers.get(field)!r} (expected {expected!r})"
            )

    section_output = _capture(
        ["readelf", "--sections", "--wide", str(path)],
        f"reading BPF ELF sections: {path}",
    )
    sections = set(
        re.findall(r"^\s*\[\s*\d+\]\s+(\S+)", section_output, re.MULTILINE)
    )
    required_sections = {"cgroup/dev", ".maps", "license"}
    missing_sections = sorted(required_sections - sections)
    if missing_sections:
        raise SystemExit(
            "BPF object is missing required sections: "
            + ", ".join(missing_sections)
        )

    symbol_output = _capture(
        ["readelf", "--symbols", "--wide", str(path)],
        f"reading BPF ELF symbols: {path}",
    )
    global_symbols = set(
        re.findall(
            r"^\s*\d+:\s+\S+\s+\d+\s+\S+\s+GLOBAL\s+\S+\s+\S+\s+(\S+)\s*$",
            symbol_output,
            flags=re.MULTILINE,
        )
    )
    required_symbols = {
        "device_reserve",
        "reserved_devices",
        "reserved_majors",
        "devdrv_major",
        "LICENSE",
    }
    missing_symbols = sorted(required_symbols - global_symbols)
    if missing_symbols:
        raise SystemExit(
            "BPF object is missing required global symbols: "
            + ", ".join(missing_symbols)
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=_project_version())
    parser.add_argument("--release", default="1")
    parser.add_argument(
        "--worker-bundle",
        type=_path,
        default=DEFAULT_BUILD / "pyinstaller-dist" / "neu-box-worker",
        help="prebuilt PyInstaller onedir bundle",
    )
    parser.add_argument(
        "--sandbox-executable",
        type=_path,
        default=DEFAULT_BUILD / "native-sandbox" / "neu-box-sandbox",
    )
    parser.add_argument(
        "--bpf-object",
        type=_path,
        default=DEFAULT_BUILD / "native-sandbox" / "device_block.o",
    )
    parser.add_argument(
        "--info-dir",
        type=_path,
        default=ROOT / "src" / "neu_box" / "worker" / "resources" / "info",
    )
    parser.add_argument(
        "--config",
        type=_path,
        default=ROOT / "deploy" / "config" / "worker.env.example",
    )
    parser.add_argument(
        "--unit",
        type=_path,
        default=ROOT / "deploy" / "systemd" / "neu-box-worker.service",
    )
    parser.add_argument(
        "--cli",
        type=_path,
        default=RPM_DIR / "assets" / "neu-box",
    )
    parser.add_argument(
        "--output-dir",
        type=_path,
        default=ROOT / "dist" / "rpm",
    )
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="only emit the rpmbuild source tarball and rendered spec",
    )
    parser.add_argument(
        "--allow-dynamic-libbpf",
        action="store_true",
        help=(
            "allow a development sandbox with a dynamic libbpf dependency"
        ),
    )
    parser.add_argument(
        "--allow-external-inputs",
        action="store_true",
        help=(
            "allow non-repository info/config/unit/CLI inputs only for a "
            "development Release ending in '.dev'"
        ),
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~]*", args.version):
        raise SystemExit("RPM version must be path-safe and must not contain '-'")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~]*", args.release):
        raise SystemExit("RPM release must be path-safe and must not contain '-'")
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise SystemExit(f"unsupported build architecture: {platform.machine()}")

    repository_inputs = {
        "device info script directory": (
            args.info_dir,
            ROOT / "src" / "neu_box" / "worker" / "resources" / "info",
        ),
        "Worker configuration": (
            args.config,
            ROOT / "deploy" / "config" / "worker.env.example",
        ),
        "systemd unit": (
            args.unit,
            ROOT / "deploy" / "systemd" / "neu-box-worker.service",
        ),
        "Worker management CLI": (
            args.cli,
            RPM_DIR / "assets" / "neu-box",
        ),
    }
    external_inputs = [
        description
        for description, (observed, expected) in repository_inputs.items()
        if observed.resolve() != expected.resolve()
    ]
    if external_inputs and not (
        getattr(args, "allow_external_inputs", False)
        and args.release.endswith(".dev")
    ):
        raise SystemExit(
            "production packaging requires repository-owned inputs; "
            "external inputs require "
            "--allow-external-inputs and a Release ending in '.dev': "
            + ", ".join(external_inputs)
        )

    _require_dir(args.worker_bundle, "PyInstaller worker bundle")
    worker = args.worker_bundle / "neu-box-worker"
    _require_file(
        worker,
        "PyInstaller worker entry point",
        executable=True,
    )
    _validate_worker_elf_architecture(
        args.worker_bundle,
        worker,
    )
    _validate_worker_version(worker, args.version)
    _require_file(args.sandbox_executable, "native sandbox", executable=True)
    _validate_native_sandbox(args.sandbox_executable)
    dynamic = _dynamic_dependencies(args.sandbox_executable)
    dynamic_libbpf = _dynamic_libbpf_dependencies(dynamic)
    if dynamic_libbpf:
        if not args.allow_dynamic_libbpf:
            raise SystemExit(
                "native sandbox dynamically requires libbpf "
                f"({', '.join(dynamic_libbpf)}); production RPM packaging "
                "refuses it (use --allow-dynamic-libbpf only for tests)"
            )
        if not args.release.endswith(".dev"):
            raise SystemExit(
                "a dynamic-libbpf development RPM must use a Release ending "
                "in '.dev' so it cannot be confused with a production build"
            )
    _require_file(args.bpf_object, "precompiled BPF object")
    if args.sandbox_executable.parent != args.bpf_object.parent:
        raise SystemExit(
            "native sandbox and device_block.o must come from the same build "
            "directory"
        )
    _validate_bpf_object(args.bpf_object)
    _require_dir(args.info_dir, "device info script directory")
    info_scripts = list(args.info_dir.glob("*_info.sh"))
    if not info_scripts:
        raise SystemExit(f"no *_info.sh scripts found in {args.info_dir}")
    _require_file(args.config, "Worker configuration")
    _require_file(args.unit, "systemd unit")
    _require_file(args.cli, "Worker management CLI")
    _require_file(ROOT / "LICENSE", "license")
    _require_file(RPM_DIR / "neu-box-worker.spec", "RPM spec")

    if not external_inputs:
        repository_paths = [args.info_dir, args.config, args.unit, args.cli]
        repository_paths.extend(info_scripts)
        symlinks = [path for path in repository_paths if path.is_symlink()]
        if symlinks:
            raise SystemExit(
                "repository-owned packaging inputs must not be symlinks: "
                + ", ".join(str(path) for path in symlinks)
            )


def _compose_source(args: argparse.Namespace, topdir: Path) -> tuple[Path, Path]:
    name = f"neu-box-worker-{args.version}"
    source_root = topdir / "source" / name
    rootfs = source_root / "rootfs"

    _copy_tree(
        args.worker_bundle,
        rootfs / "usr" / "libexec" / "neu-box" / "worker",
    )
    _copy_file(
        args.sandbox_executable,
        rootfs / "usr" / "libexec" / "neu-box" / "neu-box-sandbox",
        0o755,
    )
    _copy_file(
        args.bpf_object,
        rootfs / "usr" / "libexec" / "neu-box" / "device_block.o",
        0o644,
    )
    _copy_tree(args.info_dir, rootfs / "usr" / "share" / "neu-box" / "info")
    for script in (rootfs / "usr" / "share" / "neu-box" / "info").glob(
        "*_info.sh"
    ):
        script.chmod(0o755)
    _copy_file(args.cli, rootfs / "usr" / "sbin" / "neu-box", 0o755)
    worker_link = rootfs / "usr" / "sbin" / "neu-box-worker"
    worker_link.symlink_to("../libexec/neu-box/worker/neu-box-worker")
    _copy_file(
        args.unit,
        rootfs / "usr" / "lib" / "systemd" / "system" / "neu-box-worker.service",
        0o644,
    )
    _copy_file(args.config, rootfs / "etc" / "neu-box" / "worker.env", 0o640)
    (rootfs / "var" / "lib" / "neu-box" / "worker").mkdir(parents=True)
    _copy_file(ROOT / "LICENSE", source_root / "LICENSE", 0o644)

    sources = topdir / "SOURCES"
    specs = topdir / "SPECS"
    sources.mkdir(parents=True)
    specs.mkdir(parents=True)
    archive = sources / f"{name}-{args.release}.tar.gz"
    with archive.open("wb") as raw_archive:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_archive,
            mtime=0,
        ) as compressed_archive:
            with tarfile.open(
                fileobj=compressed_archive,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as tar:
                def normalize(member: tarfile.TarInfo) -> tarfile.TarInfo:
                    member.uid = 0
                    member.gid = 0
                    member.uname = "root"
                    member.gname = "root"
                    member.mtime = 0
                    member.pax_headers = {}
                    return member

                tar.add(source_root, arcname=name, filter=normalize)
    spec = specs / "neu-box-worker.spec"
    spec_template = (RPM_DIR / "neu-box-worker.spec").read_text(
        encoding="utf-8"
    )
    spec.write_text(
        spec_template.replace(
            "%{!?neu_box_version:%global neu_box_version 0.0.0}",
            f"%global neu_box_version {args.version}",
            1,
        ).replace(
            "%{!?neu_box_release:%global neu_box_release 1}",
            f"%global neu_box_release {args.release}",
            1,
        ),
        encoding="utf-8",
    )
    return archive, spec


def main() -> int:
    args = _parser().parse_args()
    _validate_args(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="neu-box-rpmbuild-") as raw_temp:
        topdir = Path(raw_temp)
        for directory in ("BUILD", "BUILDROOT", "RPMS", "SRPMS", "TMP"):
            (topdir / directory).mkdir()
        archive, spec = _compose_source(args, topdir)

        if args.source_only:
            output_archive = args.output_dir / archive.name
            output_spec = args.output_dir / (
                f"neu-box-worker-{args.version}-{args.release}.spec"
            )
            _publish_file(archive, output_archive)
            _publish_file(spec, output_spec)
            return 0

        command = [
            "rpmbuild",
            "-bb",
            "--define",
            f"_topdir {topdir}",
            "--define",
            f"_tmppath {topdir / 'TMP'}",
            "--define",
            f"neu_box_version {args.version}",
            "--define",
            f"neu_box_release {args.release}",
        ]
        command.append(str(spec))
        _run(command)

        packages = sorted((topdir / "RPMS").glob("**/*.rpm"))
        if not packages:
            raise SystemExit("rpmbuild completed without producing an RPM")
        for package in packages:
            destination = args.output_dir / package.name
            _publish_file(package, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
