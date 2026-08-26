"""Neu Box Worker application factory and command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys

import flask
from waitress import serve as waitress_serve

from neu_box import API_VERSION, __version__
from neu_box.config import (
    ConfigError,
    env_int,
    env_text,
    load_role_environment,
)
from neu_box.database.cli import add_database_commands, run_database_command
from neu_box.database.migrations import MigrationError, schema_status
from neu_box.logging_config import configure_logging
from neu_box.worker.executor.db import (
    MIGRATIONS_PACKAGE,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    Database,
    database_path,
)


logger = logging.getLogger("worker")


def create_app() -> flask.Flask:
    """Create the Worker API after schema validation, without root side effects."""
    Database.get_instance()
    app = flask.Flask("neu_box.worker")

    from neu_box.worker.executor.command import command_bp
    from neu_box.worker.executor.sandbox_api import sandbox_bp
    from neu_box.worker.executor.status import status_bp

    app.register_blueprint(command_bp, url_prefix="/command")
    app.register_blueprint(sandbox_bp, url_prefix="/sandbox")
    app.register_blueprint(status_bp)

    @app.get("/")
    def home():
        return {
            "service": "neu-box-worker",
            "version": __version__,
        }, 200

    @app.get("/healthz")
    def health():
        status = schema_status(database_path(), MIGRATIONS_PACKAGE)
        return {
            "status": "ok",
            "role": "worker",
            "api_version": API_VERSION,
            "version": __version__,
            "schema_version": status.current,
        }, 200

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neu-box-worker",
        description="Neu Box Worker 服务与数据库管理",
    )
    parser.add_argument(
        "--config",
        help="环境配置文件；默认使用 NEU_BOX_CONFIG 或 /etc/neu-box/worker.env",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动 Worker HTTP 服务")
    serve.add_argument("--listen", help="覆盖配置中的监听地址")
    serve.add_argument("--port", type=int, help="覆盖配置中的监听端口")
    add_database_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        load_role_environment("worker", args.config)
        if args.command == "db":
            return run_database_command(
                args,
                role="worker",
                database=database_path(),
                migrations_package=MIGRATIONS_PACKAGE,
                required_columns=REQUIRED_COLUMNS,
                required_indexes=REQUIRED_INDEXES,
            )

        configure_logging("worker")
        app = create_app()
        from neu_box.worker.executor.command import TaskQueue
        from neu_box.worker.executor.sbx_manager import SbxManager

        TaskQueue.get_instance().start()
        SbxManager.get_instance().start_reaper()
        listen = args.listen or env_text("NEU_BOX_LISTEN", "0.0.0.0", "listen")
        port = args.port or env_int("NEU_BOX_PORT", 59075, "port")
        threads = env_int("NEU_BOX_HTTP_THREADS", 8)
        logger.info("Worker 正在监听 %s:%s", listen, port)
        waitress_serve(app, host=listen, port=port, threads=threads)
        return 0
    except (ConfigError, MigrationError, OSError, ValueError) as exc:
        print(f"neu-box-worker: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
