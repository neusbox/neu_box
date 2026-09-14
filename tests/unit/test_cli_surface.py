"""CLI surface boundary: daemon vs management commands."""

import pytest

from neu_box import app, ctl


def test_daemon_only_exposes_serve():
    parser = app._parser()
    assert parser.parse_args(["serve"]).command == "serve"
    with pytest.raises(SystemExit):
        parser.parse_args(["setup"])
    with pytest.raises(SystemExit):
        parser.parse_args(["pause"])


def test_management_cli_exposes_setup_not_serve():
    parser = ctl._parser()
    assert parser.parse_args(["setup"]).command == "setup"
    assert parser.parse_args(["pause"]).command == "pause"
    with pytest.raises(SystemExit):
        parser.parse_args(["serve"])
