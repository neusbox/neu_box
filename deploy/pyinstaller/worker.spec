# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
MIGRATIONS_PACKAGE = "neu_box.worker.migrations"
# Migration discovery hashes the packaged source bytes before applying them.
# Keep Python migrations as resources and as importable hidden modules; SQL
# migrations only need the resource copy.
datas = collect_data_files(MIGRATIONS_PACKAGE, include_py_files=True)
migration_hiddenimports = collect_submodules(MIGRATIONS_PACKAGE)

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
        *migration_hiddenimports,
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
