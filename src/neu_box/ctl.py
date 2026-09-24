"""Neu Box Worker management CLI.

The daemon (``neuboxd``) and the management CLI (``neuboxctl``) are separate
entry points on purpose: ``neuboxd`` must not expose ``setup``/``pause``/
``resume``/``db`` as public subcommands.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from neu_box.config import env_int, load_role_environment, sandbox_executable_path
from neu_box.maintenance.markers import clear_pause_marker
from neu_box.maintenance.pause import SERVICE, control_worker, pause
from neu_box.maintenance.setup import setup
from neu_box.migrations.cli import add_database_commands, run_database_command
from neu_box.storage import (
    MIGRATIONS_PACKAGE,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    database_path,
)


ACCEPTANCE = Path("/usr/libexec/neu-box/tests/neu-box-deployment-tests")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neuboxctl",
        description="Neu Box Worker 管理 CLI",
    )
    parser.add_argument(
        "--config",
        help="环境配置文件；默认使用 NEU_BOX_CONFIG 或 /etc/neu-box/worker.env",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup_parser = commands.add_parser(
        "setup", help="迁移数据库、启动并检查 Worker，然后恢复调度",
    )
    setup_parser.add_argument(
        "--timeout", type=int, default=60,
        help="等待健康检查的秒数，默认 60",
    )

    pause_parser = commands.add_parser(
        "pause", help="等待任务与沙盒静止、备份、清理 BPF，最后停服",
    )
    pause_parser.add_argument(
        "--timeout", type=int, default=0,
        help="等待秒数；0 表示不限时",
    )

    commands.add_parser("resume", help="恢复仍在运行的 Worker 调度")
    add_database_commands(commands)

    sandbox = commands.add_parser(
        "sandbox", help="直接调用 native sandbox CLI", add_help=False,
    )
    sandbox.add_argument("args", nargs=argparse.REMAINDER)

    test = commands.add_parser(
        "test", help="运行部署后实机验收套件", add_help=False,
    )
    test.add_argument("args", nargs=argparse.REMAINDER)

    return parser


def _run_control(command: str, args: argparse.Namespace, config) -> int:
    if command == "resume":
        port = env_int("NEU_BOX_PORT", 59075)
        control_worker("resume", port)
        clear_pause_marker()
        return 0
    if args.timeout < 0 or (command == "setup" and args.timeout == 0):
        raise ValueError("pause 超时必须 >= 0；setup 超时必须 > 0")
    port = env_int("NEU_BOX_PORT", 59075)
    if command == "pause":
        pause(port, args.timeout, config)
    else:
        setup(None, args.timeout, config)
    return 0


def _run_database(args: argparse.Namespace) -> int:
    return run_database_command(
        args,
        role="worker",
        database=database_path(),
        migrations_package=MIGRATIONS_PACKAGE,
        required_columns=REQUIRED_COLUMNS,
        required_indexes=REQUIRED_INDEXES,
        service=SERVICE,
    )


def _exec_or_fail(path: Path, args: list[str]) -> int:
    try:
        os.execv(str(path), [str(path), *args])
    except OSError as exc:
        print(f"neuboxctl: 无法执行 {path}: {exc}", file=sys.stderr)
        return 1
    return 0  # pragma: no cover - execv 不返回


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == ["help"]:
        _parser().print_help()
        return 0

    parser = _parser()
    args = parser.parse_args(raw_argv)
    try:
        config = load_role_environment("worker", args.config)
        if args.command == "db":
            return _run_database(args)
        if args.command in {"setup", "pause", "resume"}:
            return _run_control(args.command, args, config)
        if args.command == "sandbox":
            return _exec_or_fail(sandbox_executable_path(), list(args.args))
        if args.command == "test":
            return _exec_or_fail(ACCEPTANCE, list(args.args))
        parser.error(f"未知命令: {args.command}")
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"neuboxctl: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
