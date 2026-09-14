#!/usr/bin/env python3
"""第 3 层实机验收的入口。

两种跑法都能用，行为一致：

  * 源码：``NEU_BOX_DEPLOYMENT_TESTS=1 python tests/deployment/run.py --url ...``
  * 冻结（RPM 里的样子）：``neuboxctl test [pytest 参数...]``

它做三件事：把 ``NEU_BOX_DEPLOYMENT_TESTS=1`` 设上（否则 conftest 会把整个
目录屏蔽掉，开发机上的 ``pytest tests/`` 才不会被这套用例污染）、找到套件目
录（冻结后是 ``sys._MEIPASS`` 下的 ``deployment/``）、然后把剩下的参数原样
交给 pytest。

``--self-check`` 是给打包流程用的：它证明这个产物能起来、并且 pytest 真的
在包里 —— 打 RPM 之前先跑一次，比装到部署机上才发现缺模块便宜得多。
"""

from __future__ import annotations

import os
import sys
import sysconfig


def suite_dir() -> str:
    """套件（conftest.py 与 test_*.py）所在目录。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "deployment")
    return os.path.dirname(os.path.abspath(__file__))


def _self_check() -> int:
    import pytest

    print(f"neu-box-deployment-tests: pytest {pytest.__version__}")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    print(f"suite:  {suite_dir()}")
    print(f"platform: {sysconfig.get_platform()}")
    if not os.path.isdir(suite_dir()):
        print(f"suite directory is missing: {suite_dir()}", file=sys.stderr)
        return 1
    missing = [
        name for name in ("conftest.py", "deployment_support.py", "pytest.ini")
        if not os.path.isfile(os.path.join(suite_dir(), name))
    ]
    if missing:
        print(f"suite is incomplete, missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not [name for name in os.listdir(suite_dir())
            if name.startswith("test_") and name.endswith(".py")]:
        print("suite contains no test_*.py modules", file=sys.stderr)
        return 1

    # 不仅检查文件在不在，还要真的走一遍 pytest collect。PyInstaller 漏了
    # hidden import 时文件仍然齐全，collect 阶段才会炸。
    os.environ["NEU_BOX_DEPLOYMENT_TESTS"] = "1"
    if getattr(sys, "frozen", False):
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    args = [
        suite_dir(),
        "-p", "no:cacheprovider",
        "-c", os.path.join(suite_dir(), "pytest.ini"),
        "--collect-only",
        "-q",
    ]
    print(f"+ pytest {' '.join(args)}", flush=True)
    if pytest.main(args) != 0:
        print("pytest collect failed", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--self-check"]:
        return _self_check()

    import pytest

    # 这一行必须在 conftest 被导入之前生效：它决定 conftest 收不收 test_*.py。
    os.environ["NEU_BOX_DEPLOYMENT_TESTS"] = "1"
    if getattr(sys, "frozen", False):
        # 冻进来的只有 pytest 自己，机器上装的插件不该改变验收的行为。
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    args = [
        suite_dir(),
        # 部署机上不该留下 .pytest_cache（也不一定可写）。
        "-p", "no:cacheprovider",
        # 忽略环境里的 pyproject.toml / setup.cfg / tox.ini（含 addopts）。
        "-c", os.path.join(suite_dir(), "pytest.ini"),
        "-ra",
    ]
    args.extend(argv)
    print(f"+ pytest {' '.join(args)}", flush=True)
    return int(pytest.main(args))


if __name__ == "__main__":
    raise SystemExit(main())
