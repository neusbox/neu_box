from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CGROUP_SOURCE = ROOT / 'native' / 'sandbox' / 'src' / 'cgroup.cpp'
BPF_SOURCE = ROOT / 'native' / 'sandbox' / 'src' / 'bpf.cpp'
MAIN_SOURCE = ROOT / 'native' / 'sandbox' / 'src' / 'main.cpp'
CMAKE_SOURCE = ROOT / 'native' / 'sandbox' / 'CMakeLists.txt'
BPF_PROGRAM_SOURCE = ROOT / 'native' / 'sandbox' / 'bpf' / 'device_block.bpf.c'


def test_native_sandbox_manages_cgroups_without_busctl():
    source = CGROUP_SOURCE.read_text(encoding='utf-8')

    assert 'busctl' not in source
    assert 'StartTransientUnit' not in source
    assert 'StopUnit' not in source
    assert 'fs::create_directories(path)' in source
    assert 'kill_processes(path)' in source


def test_pinned_program_is_bound_to_packaged_bpf_object_tag():
    source = BPF_SOURCE.read_text(encoding='utf-8')

    assert 'expected_program_tag(object_path)' in source
    assert 'bpf_object__load(object.get())' in source
    assert 'information.tag' in source
    assert 'pinned_tag != expected_program_tag(object_path)' in source


def test_list_validates_bpf_attachment_and_dynamic_major_before_reporting():
    main = MAIN_SOURCE.read_text(encoding='utf-8')
    bpf = BPF_SOURCE.read_text(encoding='utf-8')
    list_branch = main[main.index('if (command == "list")'):]

    assert list_branch.index('validate_bpf_list_ready(object_path)') < (
        list_branch.index('cgroup_names()')
    )
    assert 'validate_bpf_status_ready(object_path)' in bpf
    assert 'validate_device_reserve_attachments(' in bpf
    assert 'attached.flags != BPF_F_ALLOW_MULTI' in bpf
    assert 'configured_major != current_major' in bpf


def test_bpf_program_reads_devdrv_major_from_map_instead_of_hardcoding_235():
    source = BPF_PROGRAM_SOURCE.read_text(encoding='utf-8')

    assert '&devdrv_major' in source
    assert 'ctx->major != *current_devdrv_major' in source
    assert 'ctx->major != 235' not in source


def test_destroy_never_overwrites_malformed_owner_state():
    source = MAIN_SOURCE.read_text(encoding='utf-8')
    destroy = source[source.index('void destroy_one('):source.index(
        'int dispatch(',
    )]

    malformed_guard = destroy.index(
        'if (has_state && !stored_owner.has_value())'
    )
    live_branch = destroy.index('if (!has_live_cgroup)')
    overwrite = destroy.index('write_state_cgroup_id(name, live_owner)')
    assert malformed_guard < live_branch < overwrite


def test_privileged_native_helper_enables_release_hardening():
    source = CMAKE_SOURCE.read_text(encoding='utf-8')

    assert 'POSITION_INDEPENDENT_CODE ON' in source
    assert '-fstack-protector-strong' in source
    assert '_FORTIFY_SOURCE=2' in source
    assert '-Wl,-z,relro' in source
    assert '-Wl,-z,now' in source
    assert '-Wl,-z,noexecstack' in source


def test_production_build_only_requires_libbpf_static_archive():
    source = CMAKE_SOURCE.read_text(encoding='utf-8')

    assert 'NAMES libbpf.a' in source
    assert 'NAMES libelf.a' not in source
    assert 'NAMES libzstd.a' not in source
    assert 'NAMES libz.a' not in source
    assert source.index('${LIBBPF_STATIC_ARCHIVE}') < source.index(
        '${LIBBPF_RUNTIME_DEPENDENCIES}'
    )
