# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]

a = Analysis(
    [str(ROOT / "deploy" / "install.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="neu-box-install",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
