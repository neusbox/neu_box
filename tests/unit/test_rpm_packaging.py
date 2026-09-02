from __future__ import annotations

import os
import importlib.util
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
BUILD_RELEASE = ROOT / "deploy" / "build_release.py"
RPM_DIR = ROOT / "deploy" / "rpm"
BUILD_RPM = RPM_DIR / "build_rpm.py"
RPM_SPEC = RPM_DIR / "neu-box-worker.spec"
PYINSTALLER_SPEC = ROOT / "deploy" / "pyinstaller" / "worker.spec"
CLI_ASSET = RPM_DIR / "assets" / "neu-box"

_BUILD_RPM_SPEC = importlib.util.spec_from_file_location(
    "neu_box_build_rpm",
    BUILD_RPM,
)
assert _BUILD_RPM_SPEC is not None and _BUILD_RPM_SPEC.loader is not None
build_rpm_module = importlib.util.module_from_spec(_BUILD_RPM_SPEC)
_BUILD_RPM_SPEC.loader.exec_module(build_rpm_module)

_BUILD_RELEASE_SPEC = importlib.util.spec_from_file_location(
    "neu_box_build_release",
    BUILD_RELEASE,
)
assert _BUILD_RELEASE_SPEC is not None
assert _BUILD_RELEASE_SPEC.loader is not None
build_release_module = importlib.util.module_from_spec(_BUILD_RELEASE_SPEC)
_BUILD_RELEASE_SPEC.loader.exec_module(build_release_module)


def test_pyinstaller_collects_python_migrations_as_data_and_modules():
    spec = PYINSTALLER_SPEC.read_text(encoding="utf-8")

    assert "collect_submodules" in spec
    assert "include_py_files=True" in spec
    assert "*migration_hiddenimports" in spec


@pytest.fixture(scope="module")
def source_package(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """Build a source-only package from small, completely fake artifacts."""
    work = tmp_path_factory.mktemp("rpm-source")
    version = "9.8.7"
    worker_bundle = work / "worker-bundle"
    worker_bundle.mkdir()
    worker = worker_bundle / "neu-box-worker"
    worker_source = work / "worker.c"
    worker_source.write_text(
        "#include <stdio.h>\n"
        "#include <string.h>\n"
        "int main(int argc, char **argv) {\n"
        "    if (argc == 2 && strcmp(argv[1], \"--version\") == 0) {\n"
        f"        puts(\"{version}\");\n"
        "        return 0;\n"
        "    }\n"
        "    return 2;\n"
        "}\n",
        encoding="utf-8",
    )
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("source-only packaging fixture requires a C compiler")
    subprocess.run(
        [compiler, str(worker_source), "-o", str(worker)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    internal = worker_bundle / "_internal"
    internal.mkdir()
    (internal / "runtime.bin").write_bytes(b"fake-pyinstaller-runtime")

    sandbox = work / "neu-box-sandbox"
    sandbox.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --help ]; then\n"
        "  printf '%s\\n' '--bpf-object PATH' "
        "'create <name> <cpu> <mem>' 'join <name> <PID>' "
        "cleanup\n"
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    sandbox.chmod(0o755)
    bpf_object = work / "device_block.o"
    bpf_object.write_bytes(b"fake-bpf-object")

    # Keep this layout test independent from the host architecture while still
    # exercising build_rpm.py's strict ELF/BPF metadata checks.
    native_machine = {
        "aarch64": "AArch64",
        "x86_64": "Advanced Micro Devices X86-64",
    }[build_rpm_module.platform.machine()]
    tools_dir = work / "tools"
    tools_dir.mkdir()
    readelf = tools_dir / "readelf"
    readelf.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  --dynamic) exit 0 ;;\n"
        "  --file-header)\n"
        "    if [ \"${2##*/}\" = device_block.o ]; then\n"
        "      printf '%s\\n' '  Class: ELF64' "
        "\"  Data: 2's complement, little endian\" "
        "'  Type: REL (Relocatable file)' '  Machine: Linux BPF'\n"
        "    else\n"
        "      printf '%s\\n' '  Class: ELF64' "
        "\"  Data: 2's complement, little endian\" "
        "'  Type: EXEC (Executable file)' "
        f"'  Machine: {native_machine}'\n"
        "    fi ;;\n"
        "  --sections)\n"
        "    printf '%s\\n' '  [ 1] cgroup/dev PROGBITS' "
        "'  [ 2] .maps PROGBITS' '  [ 3] license PROGBITS' ;;\n"
        "  --symbols)\n"
        "    printf '%s\\n' "
        "'  1: 0 1 FUNC GLOBAL DEFAULT 1 device_reserve' "
        "'  2: 0 1 OBJECT GLOBAL DEFAULT 2 reserved_devices' "
        "'  3: 0 1 OBJECT GLOBAL DEFAULT 2 reserved_majors' "
        "'  4: 0 1 OBJECT GLOBAL DEFAULT 2 devdrv_major' "
        "'  5: 0 1 OBJECT GLOBAL DEFAULT 3 LICENSE' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    readelf.chmod(0o755)

    info_dir = work / "info"
    info_dir.mkdir()
    for name in ("gpu_info.sh", "npu_info.sh"):
        script = info_dir / name
        script.write_text(f"#!/bin/sh\necho {name}\n", encoding="utf-8")
        # build_rpm.py is responsible for making packaged info scripts executable.
        script.chmod(0o644)

    config = work / "worker.env"
    config.write_text(
        "NEU_BOX_DB_PATH=/var/lib/neu-box/worker/neu_box.db\n",
        encoding="utf-8",
    )
    unit = work / "neu-box-worker.service"
    unit.write_text(
        "[Service]\n"
        "ExecStart=/usr/libexec/neu-box/worker/neu-box-worker serve\n",
        encoding="utf-8",
    )
    cli = work / "neu-box"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)

    output = work / "output"
    environment = os.environ.copy()
    environment["PATH"] = f"{tools_dir}:{environment['PATH']}"
    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_RPM),
            "--source-only",
            "--version",
            version,
            "--release",
            "4.dev",
            "--worker-bundle",
            str(worker_bundle),
            "--sandbox-executable",
            str(sandbox),
            "--bpf-object",
            str(bpf_object),
            "--info-dir",
            str(info_dir),
            "--config",
            str(config),
            "--unit",
            str(unit),
            "--cli",
            str(cli),
            "--output-dir",
            str(output),
            "--allow-external-inputs",
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    archive = output / f"neu-box-worker-{version}-4.dev.tar.gz"
    assert archive.is_file()
    assert (output / f"neu-box-worker-{version}-4.dev.spec").is_file()
    return archive, f"neu-box-worker-{version}"


def test_source_only_stages_complete_standard_layout(source_package):
    archive, package_root = source_package
    prefix = f"{package_root}/rootfs"
    expected_files = {
        f"{prefix}/usr/libexec/neu-box/worker/neu-box-worker": 0o755,
        f"{prefix}/usr/libexec/neu-box/worker/_internal/runtime.bin": None,
        f"{prefix}/usr/libexec/neu-box/neu-box-sandbox": 0o755,
        f"{prefix}/usr/libexec/neu-box/device_block.o": 0o644,
        f"{prefix}/usr/share/neu-box/info/gpu_info.sh": 0o755,
        f"{prefix}/usr/share/neu-box/info/npu_info.sh": 0o755,
        f"{prefix}/usr/sbin/neu-box": 0o755,
        f"{prefix}/usr/lib/systemd/system/neu-box-worker.service": 0o644,
        f"{prefix}/etc/neu-box/worker.env": 0o640,
        f"{package_root}/LICENSE": 0o644,
    }

    with tarfile.open(archive, "r:gz") as stream:
        members = {member.name: member for member in stream.getmembers()}
        for name, mode in expected_files.items():
            assert name in members
            assert members[name].isfile()
            if mode is not None:
                assert members[name].mode & 0o777 == mode

        worker_link = members[f"{prefix}/usr/sbin/neu-box-worker"]
        assert worker_link.issym()
        assert worker_link.linkname == "../libexec/neu-box/worker/neu-box-worker"
        assert f"{prefix}/var/lib/neu-box/worker" in members
        assert f"{prefix}/var/lib/neu-box/worker/task-logs" not in members


def test_source_only_uses_supplied_config_unit_and_cli(source_package):
    archive, package_root = source_package
    prefix = f"{package_root}/rootfs"
    expected = {
        f"{prefix}/etc/neu-box/worker.env": (
            b"NEU_BOX_DB_PATH=/var/lib/neu-box/worker/neu_box.db\n"
        ),
        f"{prefix}/usr/lib/systemd/system/neu-box-worker.service": (
            b"[Service]\n"
            b"ExecStart=/usr/libexec/neu-box/worker/neu-box-worker serve\n"
        ),
        f"{prefix}/usr/sbin/neu-box": b"#!/bin/sh\nexit 0\n",
    }

    with tarfile.open(archive, "r:gz") as stream:
        for name, content in expected.items():
            packaged = stream.extractfile(name)
            assert packaged is not None
            assert packaged.read() == content


def test_source_package_contains_no_legacy_opt_layout(source_package):
    archive, package_root = source_package
    rootfs = f"{package_root}/rootfs/"

    with tarfile.open(archive, "r:gz") as stream:
        payload = [
            member for member in stream.getmembers()
            if member.name.startswith(rootfs)
        ]
        assert not any(
            member.name == f"{package_root}/rootfs/opt"
            or member.name.startswith(f"{package_root}/rootfs/opt/")
            for member in payload
        )
        for member in payload:
            if not member.isfile():
                continue
            packaged = stream.extractfile(member)
            assert packaged is not None
            assert b"/opt/neu-box" not in packaged.read()


def test_source_only_renders_requested_version_and_release(source_package):
    archive, package_root = source_package
    spec = (archive.parent / f"{package_root}-4.dev.spec").read_text(
        encoding="utf-8"
    )

    assert "%global neu_box_version 9.8.7" in spec
    assert "%global neu_box_release 4.dev" in spec
    assert "%global neu_box_version 0.0.0" not in spec
    assert "%{?dist}" not in spec


def test_packager_rejects_worker_bundle_version_mismatch(tmp_path):
    worker = tmp_path / "neu-box-worker"
    worker.write_text("#!/bin/sh\nprintf '0.4.1\\n'\n", encoding="utf-8")
    worker.chmod(0o755)

    with pytest.raises(SystemExit, match="does not match RPM Version"):
        build_rpm_module._validate_worker_version(worker, "0.5.0")


def test_packager_rejects_bpf_object_missing_required_symbol(
    tmp_path,
    monkeypatch,
):
    bpf_object = tmp_path / "device_block.o"
    bpf_object.touch()

    def fake_capture(command, _description, **_kwargs):
        if "--file-header" in command:
            return (
                "Class: ELF64\n"
                "Data: 2's complement, little endian\n"
                "Type: REL (Relocatable file)\n"
                "Machine: Linux BPF\n"
            )
        if "--sections" in command:
            return "[ 1] cgroup/dev X\n[ 2] .maps X\n[ 3] license X\n"
        if "--symbols" in command:
            return (
                "1: 0 1 FUNC GLOBAL DEFAULT 1 device_reserve\n"
                "2: 0 1 OBJECT GLOBAL DEFAULT 2 reserved_devices\n"
                "3: 0 1 OBJECT GLOBAL DEFAULT 2 reserved_majors\n"
                "4: 0 1 OBJECT GLOBAL DEFAULT 3 LICENSE\n"
            )
        raise AssertionError(command)

    monkeypatch.setattr(build_rpm_module, "_capture", fake_capture)
    with pytest.raises(SystemExit, match="devdrv_major"):
        build_rpm_module._validate_bpf_object(bpf_object)


def test_packager_rejects_native_sandbox_for_wrong_architecture(
    tmp_path,
    monkeypatch,
):
    sandbox = tmp_path / "neu-box-sandbox"
    sandbox.touch()
    expected_machine = (
        "AArch64"
        if build_rpm_module.platform.machine() == "x86_64"
        else "Advanced Micro Devices X86-64"
    )
    monkeypatch.setattr(
        build_rpm_module,
        "_elf_headers",
        lambda *_args: {
            "Class": "ELF64",
            "Data": "2's complement, little endian",
            "Type": "EXEC (Executable file)",
            "Machine": expected_machine,
        },
    )

    with pytest.raises(SystemExit, match="unexpected Machine"):
        build_rpm_module._validate_native_sandbox(sandbox)


def test_packager_rejects_native_sandbox_without_join_command(
    tmp_path,
    monkeypatch,
):
    sandbox = tmp_path / "neu-box-sandbox"
    sandbox.touch()
    machine = {
        "aarch64": "AArch64",
        "x86_64": "Advanced Micro Devices X86-64",
    }[build_rpm_module.platform.machine()]
    monkeypatch.setattr(
        build_rpm_module,
        "_elf_headers",
        lambda *_args: {
            "Class": "ELF64",
            "Data": "2's complement, little endian",
            "Type": "EXEC (Executable file)",
            "Machine": machine,
        },
    )
    monkeypatch.setattr(
        build_rpm_module,
        "_capture",
        lambda *_args, **_kwargs: (
            "--bpf-object PATH\n"
            "create <name> <cpu> <mem>\n"
            "cleanup\n"
        ),
    )

    with pytest.raises(SystemExit, match="does not identify the expected CLI"):
        build_rpm_module._validate_native_sandbox(sandbox)


def test_worker_elf_scan_rejects_wrong_architecture_under_internal(
    tmp_path,
    monkeypatch,
):
    bundle = tmp_path / "neu-box-worker"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    worker = bundle / "neu-box-worker"
    worker.write_bytes(b"\x7fELFworker")
    wrong_library = internal / "wrong.so"
    wrong_library.write_bytes(b"\x7fELFlibrary")
    host_machine = {
        "aarch64": "AArch64",
        "x86_64": "Advanced Micro Devices X86-64",
    }[build_rpm_module.platform.machine()]
    wrong_machine = (
        "AArch64"
        if host_machine == "Advanced Micro Devices X86-64"
        else "Advanced Micro Devices X86-64"
    )

    def headers(path, _description):
        return {
            "Class": "ELF64",
            "Data": "2's complement, little endian",
            "Type": "DYN (Shared object file)",
            "Machine": wrong_machine if path == wrong_library else host_machine,
        }

    monkeypatch.setattr(build_rpm_module, "_elf_headers", headers)
    with pytest.raises(SystemExit, match=r"_internal/wrong\.so.*Machine"):
        build_rpm_module._validate_worker_elf_architecture(bundle, worker)


def test_worker_bundle_rejects_external_symlink(tmp_path):
    bundle = tmp_path / "neu-box-worker"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    outside = tmp_path / "host-library.so"
    outside.write_bytes(b"\x7fELFhost")
    (internal / "library.so").symlink_to(outside)

    with pytest.raises(SystemExit, match="absolute symlink|escapes the bundle"):
        build_rpm_module._validate_bundle_symlink(
            bundle,
            internal / "library.so",
            os.readlink(internal / "library.so"),
        )


def test_native_release_build_starts_from_clean_build_tree(
    tmp_path,
    monkeypatch,
):
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        build_release_module.shutil,
        "rmtree",
        lambda path, **kwargs: events.append(("remove", (path, kwargs))),
    )
    monkeypatch.setattr(
        build_release_module,
        "_run",
        lambda command, **_kwargs: events.append(("run", command)),
    )

    build_release_module._build_native(tmp_path, static_libbpf=True)

    assert events[0] == ("remove", (tmp_path, {"ignore_errors": True}))
    assert [event[0] for event in events[1:]] == ["run", "run", "run"]


def test_worker_release_build_starts_from_clean_dist_tree(tmp_path, monkeypatch):
    events: list[tuple[str, object]] = []
    dist = tmp_path / "dist"
    work = tmp_path / "work"
    monkeypatch.setattr(
        build_release_module.shutil,
        "rmtree",
        lambda path, **kwargs: events.append(("remove", (path, kwargs))),
    )
    monkeypatch.setattr(
        build_release_module,
        "_run",
        lambda command, **_kwargs: events.append(("run", command)),
    )

    build_release_module._build_worker(dist, work)

    assert events[0] == ("remove", (dist, {"ignore_errors": True}))
    assert events[1] == ("remove", (work, {"ignore_errors": True}))
    assert events[2][0] == "run"


def test_production_packager_rejects_external_repository_inputs(tmp_path):
    args = SimpleNamespace(
        version="0.5.0",
        release="1",
        info_dir=tmp_path / "info",
        config=tmp_path / "worker.env",
        unit=tmp_path / "worker.service",
        cli=tmp_path / "neu-box",
        allow_external_inputs=True,
    )

    with pytest.raises(SystemExit, match=r"Release ending in '\.dev'"):
        build_rpm_module._validate_args(args)


def test_release_output_is_atomically_replaced(tmp_path):
    source = tmp_path / "new.rpm"
    source.write_bytes(b"same")
    destination = tmp_path / "dist" / "neu-box-worker.rpm"

    build_rpm_module._publish_file(source, destination)
    assert destination.read_bytes() == b"same"

    source.write_bytes(b"different")
    build_rpm_module._publish_file(source, destination)
    assert destination.read_bytes() == b"different"


def test_packager_requires_dev_release_for_dynamic_libbpf(monkeypatch, tmp_path):
    monkeypatch.setattr(build_rpm_module, "_require_file", lambda *_a, **_k: None)
    monkeypatch.setattr(build_rpm_module, "_require_dir", lambda *_a, **_k: None)
    monkeypatch.setattr(build_rpm_module, "_validate_worker_version", lambda *_a: None)
    monkeypatch.setattr(
        build_rpm_module,
        "_validate_worker_elf_architecture",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        build_rpm_module,
        "_validate_native_sandbox",
        lambda *_a: None,
    )
    monkeypatch.setattr(build_rpm_module, "_validate_bpf_object", lambda *_a: None)
    monkeypatch.setattr(
        build_rpm_module,
        "_dynamic_dependencies",
        lambda *_a: "Shared library: [libbpf.so.1]",
    )
    monkeypatch.setattr(build_rpm_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        Path,
        "glob",
        lambda _self, _pattern: iter([tmp_path / "npu_info.sh"]),
    )
    args = SimpleNamespace(
        version="0.5.0",
        release="1",
        worker_bundle=tmp_path,
        sandbox_executable=tmp_path / "neu-box-sandbox",
        bpf_object=tmp_path / "device_block.o",
        info_dir=(
            ROOT / "src" / "neu_box" / "worker" / "resources" / "info"
        ),
        config=ROOT / "deploy" / "config" / "worker.env.example",
        unit=ROOT / "deploy" / "systemd" / "neu-box-worker.service",
        cli=RPM_DIR / "assets" / "neu-box",
        allow_dynamic_libbpf=True,
        allow_external_inputs=False,
    )

    with pytest.raises(SystemExit, match="Release ending in '.dev'"):
        build_rpm_module._validate_args(args)


def test_packager_only_treats_dynamic_libbpf_as_development_only():
    dynamic = "\n".join([
        "Shared library: [libbpf.so.1]",
        "Shared library: [libelf.so.1]",
        "Shared library: [libzstd.so.1]",
        "Shared library: [libz.so.1]",
        "Shared library: [libc.so.6]",
    ])

    assert build_rpm_module._dynamic_libbpf_dependencies(dynamic) == [
        "libbpf.so.1",
    ]


def _scriptlets(spec: str) -> dict[str, str]:
    matches = re.finditer(
        r"^%(pre|post|preun|postun)(?:[ \t][^\n]*)?\n"
        r"(.*?)(?=^%[A-Za-z]|\Z)",
        spec,
        flags=re.MULTILINE | re.DOTALL,
    )
    return {match.group(1): match.group(2) for match in matches}


def _commands(scriptlet: str) -> str:
    return "\n".join(
        line for line in scriptlet.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_spec_preserves_config_and_keeps_scriptlets_policy_free():
    spec = RPM_SPEC.read_text(encoding="utf-8")
    assert re.search(
        r"^%config\(noreplace\).*%\{_sysconfdir\}/neu-box/worker\.env$",
        spec,
        flags=re.MULTILINE,
    )
    assert "/opt/neu-box" not in spec
    assert (
        '__requires_exclude_from '
        '^%{_libexecdir}/neu-box/worker/_internal/.*$'
    ) in spec
    assert (
        '__requires_exclude_from '
        '^%{_libexecdir}/neu-box/worker/.*$'
    ) not in spec

    scriptlets = _scriptlets(spec)
    assert set(scriptlets) == {"pre", "post", "preun", "postun"}
    commands = "\n".join(_commands(body) for body in scriptlets.values())
    assert not re.search(
        r"\bsystemctl\s+(?:enable|start|restart)\b",
        commands,
    )
    assert not re.search(r"\bdb\s+(?:migrate|backup|check)\b", commands)
    assert "cleanup" not in commands
    assert "/proc/" not in commands
    assert "/sys/fs/cgroup" not in commands
    assert "/sys/fs/bpf" not in commands
    assert "DropInPaths" not in commands
    assert "FragmentPath" not in commands
    assert "dnf" not in commands.lower()
    post_commands = _commands(scriptlets["post"])
    assert post_commands == (
        "/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :"
    )
    assert "exit 1" not in post_commands

    pre_commands = _commands(scriptlets["pre"])
    assert "/usr/bin/systemctl is-active --quiet" in pre_commands
    assert "neu-box-worker.service" in pre_commands
    assert "stop it before installing this RPM" in pre_commands
    assert "exit 1" in pre_commands
    assert "legacy Worker" not in spec
    assert "v0.4.1" not in spec
    assert "/usr/local/sbin/neu-box-install" not in spec

    preun_commands = _commands(scriptlets["preun"])
    assert 'if [ "$1" -eq 0 ]' in preun_commands
    assert "/usr/bin/systemctl is-active --quiet" in preun_commands
    assert "stop it before erasing this RPM" in preun_commands
    assert (
        "/usr/bin/systemctl disable neu-box-worker.service "
        ">/dev/null 2>&1 || :"
    ) in preun_commands


def test_vendor_unit_owns_worker_data_mount_dependency():
    unit = (
        ROOT / 'deploy' / 'systemd' / 'neu-box-worker.service'
    ).read_text(encoding='utf-8')

    assert 'RequiresMountsFor=/var/lib/neu-box' in unit


@pytest.fixture
def runnable_cli(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Relocate the packaged CLI constants so it can call a fake Worker."""
    record = tmp_path / "worker-arguments"
    worker = tmp_path / "neu-box-worker"
    worker.write_text(
        "#!/bin/sh\n"
        "if [ -n \"${NEU_BOX_CLI_UMASK_RECORD:-}\" ]; then\n"
        "  umask >\"$NEU_BOX_CLI_UMASK_RECORD\"\n"
        "fi\n"
        "printf '%s\\n' \"$@\" >\"$NEU_BOX_CLI_RECORD\"\n",
        encoding="utf-8",
    )
    worker.chmod(0o755)
    sandbox = tmp_path / "neu-box-sandbox"
    sandbox.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" >\"$NEU_BOX_CLI_RECORD\"\n",
        encoding="utf-8",
    )
    sandbox.chmod(0o755)
    config = tmp_path / "worker.env"
    config.write_text("TEST_CONFIG=1\n", encoding="utf-8")

    script = CLI_ASSET.read_text(encoding="utf-8")
    script = script.replace(
        "readonly WORKER=/usr/libexec/neu-box/worker/neu-box-worker",
        f"readonly WORKER={worker}",
    ).replace(
        "readonly SANDBOX=/usr/libexec/neu-box/neu-box-sandbox",
        f"readonly SANDBOX={sandbox}",
    ).replace(
        "readonly CONFIG=/etc/neu-box/worker.env",
        f"readonly CONFIG={config}",
    )
    cli = tmp_path / "neu-box"
    cli.write_text(script, encoding="utf-8")
    cli.chmod(0o755)
    return cli, config, record


def _run_cli(cli: Path, record: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["NEU_BOX_CLI_RECORD"] = str(record)
    return subprocess.run(
        [str(cli), *arguments],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )


def test_cli_help_does_not_invoke_worker(runnable_cli):
    cli, _config, record = runnable_cli
    result = _run_cli(cli, record, "--help")

    assert result.returncode == 0, result.stderr
    assert "neu-box <命令>" in result.stdout
    assert "db <status|migrate|check|backup>" in result.stdout
    assert not record.exists()


def test_cli_version_delegates_to_worker(runnable_cli):
    cli, _config, record = runnable_cli
    result = _run_cli(cli, record, "version")

    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8").splitlines() == ["--version"]


def test_cli_passes_config_and_all_db_arguments_to_worker(runnable_cli, tmp_path):
    cli, config, record = runnable_cli
    backup_dir = tmp_path / "backups"
    result = _run_cli(
        cli,
        record,
        "db",
        "backup",
        "--output-dir",
        str(backup_dir),
    )

    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8").splitlines() == [
        "--config",
        str(config),
        "db",
        "backup",
        "--output-dir",
        str(backup_dir),
    ]


def test_cli_applies_private_umask_before_database_commands(
    runnable_cli,
    tmp_path,
):
    cli, _config, record = runnable_cli
    umask_record = tmp_path / "worker-umask"
    environment = os.environ.copy()
    environment["NEU_BOX_CLI_RECORD"] = str(record)
    environment["NEU_BOX_CLI_UMASK_RECORD"] = str(umask_record)

    result = subprocess.run(
        [str(cli), "db", "status"],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert umask_record.read_text(encoding="utf-8").strip() == "0077"


def test_cli_delegates_sandbox_arguments(runnable_cli):
    cli, _config, record = runnable_cli
    result = _run_cli(cli, record, "sandbox", "cleanup")

    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8").splitlines() == ["cleanup"]
