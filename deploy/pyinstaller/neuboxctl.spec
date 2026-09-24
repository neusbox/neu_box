# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
MIGRATIONS_PACKAGE = "neu_box.migrations"

# DB migration runs from the frozen management CLI.  Keep both the SQL/Python
# resources and the importable migration modules in the bundle.
datas = collect_data_files(MIGRATIONS_PACKAGE, include_py_files=True)
migration_hiddenimports = collect_submodules(MIGRATIONS_PACKAGE)

a = Analysis(
    [str(SRC / "neu_box" / "ctl.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "neu_box.maintenance.pause",
        "neu_box.maintenance.setup",
        "neu_box.migrations.cli",
        "neu_box.migrations.engine",
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
    name="neuboxctl",
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
    name="neuboxctl",
)
