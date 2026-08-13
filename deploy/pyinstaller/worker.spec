# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
datas = [
    item for item in collect_data_files("neu_box.worker")
    if not item[0].endswith("device_block.o")
]
bpf_object = os.environ.get("NEU_BOX_BUILD_BPF_OBJECT")
if not bpf_object:
    raise RuntimeError("NEU_BOX_BUILD_BPF_OBJECT is required")
datas.append((bpf_object, "neu_box/worker/resources/sandbox/v2"))

a = Analysis(
    [str(SRC / "neu_box" / "worker" / "app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "neu_box.worker.executor.command",
        "neu_box.worker.executor.command_docker",
        "neu_box.worker.executor.command_target",
        "neu_box.worker.executor.sandbox_api",
        "neu_box.worker.executor.sbx_manager",
        "neu_box.worker.executor.status",
        "neu_box.worker.migrations",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["bcrypt"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="neu-box-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="neu-box-worker",
)
