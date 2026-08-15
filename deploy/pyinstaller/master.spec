# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


ROOT = Path(SPECPATH).resolve().parents[1]
SRC = ROOT / "src"
datas = collect_data_files("neu_box.master")

a = Analysis(
    [str(SRC / "neu_box" / "master" / "app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "neu_box.master.api.auth",
        "neu_box.master.api.command",
        "neu_box.master.api.experiment",
        "neu_box.master.api.nodes",
        "neu_box.master.migrations",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["docker", "psutil"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="neu-box-master",
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
    name="neu-box-master",
)

