"""Neu Box Worker daemon application factory and command-line entry point.

This module is daemon-only.  Management operations (``setup``/``pause``/
``resume``/``db``/``sandbox``/``test``) live in :mod:`neu_box.ctl` and are exposed
through the separate ``neuboxctl`` executable.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys

import flask
from waitress import serve as waitress_serve

from neu_box import API_VERSION, __version__
from neu_box.config import env_int, env_text, load_role_environment
from neu_box.logging_config import configure_logging
from neu_box.maintenance.markers import pause_marker
from neu_box.migrations.engine import schema_status
from neu_box.storage import (
    MIGRATIONS_PACKAGE,
    Database,
    database_path,
)


logger = logging.getLogger("neuboxd")


def create_app() -> flask.Flask:
    """Create the Worker API after schema validation, without root side effects."""
    Database.get_instance()
    app = flask.Flask("neu_box")
    app.json.ensure_ascii = False

    from neu_box.api.tasks import command_bp
    from neu_box.api.sandboxes import sandbox_bp
    from neu_box.api.status import status_bp
    from neu_box.api.maintenance import maintenance_bp
    from neu_box.api.containers import container_bp

    app.register_blueprint(command_bp, url_prefix="/tasks")
    app.register_blueprint(sandbox_bp, url_prefix="/sandbox")
    app.register_blueprint(container_bp, url_prefix="/container")
    app.register_blueprint(status_bp)
    app.register_blueprint(maintenance_bp)

    @app.get("/")
    def home():
        return {
            "service": "neuboxd",
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
        prog="neuboxd",
        description="Neu Box Worker 守护进程",
    )
    parser.add_argument(
        "--config",
        help="环境配置文件；默认使用 NEU_BOX_CONFIG 或 /etc/neu-box/worker.env",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="启动 Worker HTTP 服务")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        load_role_environment("worker", args.config)
        configure_logging("neuboxd")
        app = create_app()

        from neu_box.scheduling.queue import TaskQueue
        from neu_box.runtime.sandbox import SbxManager

        sbx = SbxManager.get_instance()
        sbx.load_bpf()
        queue = TaskQueue.get_instance()
        # ``pause`` persists this marker before stopping the service.  A
        # restart between pause and setup must remain allocation-paused until
        # setup's health check explicitly resumes it.
        if pause_marker().exists():
            queue.set_paused(True)
            logger.warning("检测到维护标记，Worker 保持暂停等待 neuboxctl setup")
        queue.start()
        sbx.reaper.start()
        listen = env_text("NEU_BOX_LISTEN", "0.0.0.0")
        port = env_int("NEU_BOX_PORT", 59075)
        threads = env_int("NEU_BOX_HTTP_THREADS", 8)
        logger.info("Worker 正在监听 %s:%s", listen, port)
        waitress_serve(app, host=listen, port=port, threads=threads)
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"neuboxd: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已中断，不会自动恢复调度或强制清理。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
