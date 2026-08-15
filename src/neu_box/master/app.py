"""Neu Box Master application factory and command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from datetime import timedelta

import flask
from waitress import serve as waitress_serve

from neu_box import __version__
from neu_box.config import (
    ConfigError,
    env_int,
    env_text,
    load_role_environment,
)
from neu_box.database.cli import add_database_commands, run_database_command
from neu_box.database.migrations import MigrationError, schema_status
from neu_box.logging_config import configure_logging
from neu_box.master.paths import nodes_config_path, static_dir, uploads_dir
from neu_box.master.services.db import (
    MIGRATIONS_PACKAGE,
    REQUIRED_COLUMNS,
    REQUIRED_INDEXES,
    Database,
    database_path,
)


logger = logging.getLogger("master")


def create_app() -> flask.Flask:
    """Create the Flask app after configuration and schema validation."""
    nodes_path = nodes_config_path()
    if not nodes_path.is_file():
        raise ConfigError(
            f"节点配置不存在: {nodes_path}；请从 nodes.json.example 初始化"
        )
    Database.get_instance()

    app = flask.Flask(
        "neu_box.master",
        static_folder=str(static_dir()),
        static_url_path="/static",
    )
    raw_secret = env_text("SECRET_KEY")
    app.secret_key = raw_secret if raw_secret else secrets.token_hex(32)
    if not raw_secret:
        logger.warning("未设置 SECRET_KEY，重启后所有用户需要重新登录")
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Import blueprints only after environment loading and schema validation.
    from neu_box.master.api.auth import auth_bp
    from neu_box.master.api.command import command_bp
    from neu_box.master.api.experiment import experiment_bp
    from neu_box.master.api.nodes import nodes_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(command_bp, url_prefix="/command")
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    app.register_blueprint(experiment_bp, url_prefix="/experiments")

    @app.get("/")
    def home():
        return flask.send_from_directory(app.static_folder, "index.html")

    @app.get("/static/uploads/<path:filename>")
    def uploaded_file(filename: str):
        return flask.send_from_directory(str(uploads_dir()), filename)

    @app.get("/healthz")
    def health():
        status = schema_status(database_path(), MIGRATIONS_PACKAGE)
        return {
            "status": "ok",
            "role": "master",
            "version": __version__,
            "schema_version": status.current,
        }, 200

    return app


def _init_admin() -> None:
    db = Database.get_instance()
    admin_user = env_text("ADMIN_USER", "admin")
    admin_pass = env_text("ADMIN_PASS", "admin")
    if db.get_user(admin_user):
        logger.info("管理员账号已存在: %s", admin_user)
        return
    uid = db.create_user(admin_user, admin_pass, role="admin")
    if uid:
        logger.info(
            "已创建管理员: %s (%s)",
            admin_user,
            "密码来自 ADMIN_PASS" if os.getenv("ADMIN_PASS") else "使用默认密码",
        )
    else:
        logger.warning("创建管理员失败（可能已存在）")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neu-box-master",
        description="Neu Box Master 服务与数据库管理",
    )
    parser.add_argument(
        "--config",
        help="环境配置文件；默认使用 NEU_BOX_CONFIG 或 /etc/neu-box/master.env",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动 Master HTTP 服务")
    serve.add_argument("--listen", help="覆盖配置中的监听地址")
    serve.add_argument("--port", type=int, help="覆盖配置中的监听端口")
    add_database_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        load_role_environment("master", args.config)
        if args.command == "db":
            return run_database_command(
                args,
                role="master",
                database=database_path(),
                migrations_package=MIGRATIONS_PACKAGE,
                required_columns=REQUIRED_COLUMNS,
                required_indexes=REQUIRED_INDEXES,
            )

        configure_logging("master")
        app = create_app()
        _init_admin()
        from neu_box.master.services.nodes_pool import Nodes_Pool

        pool = Nodes_Pool.get_nodes_pool()
        pool.start_polling(interval=env_int("NEU_BOX_POLL_INTERVAL", 15, "poll_interval"))
        listen = args.listen or env_text("NEU_BOX_LISTEN", "0.0.0.0", "listen")
        port = args.port or env_int("NEU_BOX_PORT", 25565, "port")
        threads = env_int("NEU_BOX_HTTP_THREADS", 8)
        logger.info("Master 正在监听 %s:%s", listen, port)
        try:
            waitress_serve(app, host=listen, port=port, threads=threads)
        finally:
            pool.stop_polling()
        return 0
    except (ConfigError, MigrationError, OSError, ValueError) as exc:
        print(f"neu-box-master: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

