# -*- mode: python ; coding: utf-8 -*-
"""第 3 层实机验收套件的 PyInstaller 规格。

冻结的是**跑测试的运行时**（pytest + 套件源码），不是被测对象：目标机装完
worker RPM 就够了，不需要 Python 环境。

两个要点：

* ``test_*.py`` / ``conftest.py`` / ``deployment_support.py`` 以 **datas** 的
  形式原样放进 ``_internal/deployment/``。pytest 要靠源文件做断言重写和收集，
  打进 PYZ 就找不到了。
* ``_pytest`` 的子模块是动态导入的，静态分析看不出来，必须
  ``collect_submodules``；``run.py`` 里再关掉插件自动加载（插件本来也没打
  进来），免得部署机上的 site-packages 影响验收行为。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
SUITE = ROOT / "tests" / "deployment"

# 套件源码：run.py 是入口本身（已被 Analysis 编译），其余全部原样放在
# _internal/deployment/ 下供 pytest 收集；pytest.ini 由 run.py 用 -c 指
# 定，把环境里的 pyproject.toml 挡在外面。
datas = [
    (str(path), "deployment")
    for path in sorted(SUITE.glob("*.py"))
    if path.name != "run.py"
]
datas.append((str(SUITE / "pytest.ini"), "deployment"))
if not datas:
    raise SystemExit(f"no acceptance suite sources found in {SUITE}")
for source, _destination in datas:
    if not Path(source).is_file():
        raise SystemExit(f"acceptance suite source is missing: {source}")

a = Analysis(
    [str(SUITE / "run.py")],
    pathex=[str(SUITE)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # 插件/断言重写/终端输出都走动态导入。
        *collect_submodules("_pytest"),
        "pytest",
        # pytest 的可选依赖：装不上就走降级路径，显式列出来让打包期就能
        # 看见是谁缺了，而不是等到部署机上才发现。
        "pygments",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["bcrypt"],
    noarchive=False,
    # 不要 optimize：被 -O 去掉的 assert 会让一部分用例静默失效。
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="neu-box-deployment-tests",
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
    name="neu-box-deployment-tests",
)
