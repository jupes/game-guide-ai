"""The request-body ceiling's settings and its nginx twin (agent-forge-harness-ust7).

`service/body_limit.py` refuses a body above `config.REQUEST_BODY_MAX_BYTES`
(1 MiB unless set) before any route reads it. `ui/nginx.conf` declares the same
default at server level, where every location without its own
`client_max_body_size` inherits it. The API docs setting is off unless set, and
ignored on Cloud Run.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

import config

NGINX_CONF = Path(__file__).resolve().parent.parent / "ui" / "nginx.conf"
_LOCATION = re.compile(r"^\s*location\s", re.MULTILINE)
_SIZE = re.compile(r"\bclient_max_body_size\s+(\d+)([kKmM]?)\s*;")
_UNIT = {"": 1, "k": 1024, "m": 1024 * 1024}
_SETTINGS = ("RAG_REQUEST_BODY_MAX_BYTES", "RAG_API_DOCS_ENABLED", "K_SERVICE")


def teardown_module(_module):
    importlib.reload(config)


def _server_level(conf: str) -> str:
    """The server block's own directives: everything before its first location,
    with comments removed."""
    code = "\n".join(line.split("#", 1)[0] for line in conf.splitlines())
    first = _LOCATION.search(code)
    assert first is not None
    return code[: first.start()]


def test_nginx_declares_the_service_default_at_server_level() -> None:
    sizes = _SIZE.findall(_server_level(NGINX_CONF.read_text(encoding="utf-8")))
    assert len(sizes) == 1, "exactly one server-level client_max_body_size"
    number, unit = sizes[0]
    assert int(number) * _UNIT[unit.lower()] == config.DEFAULT_REQUEST_BODY_MAX_BYTES


def test_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SETTINGS:
        monkeypatch.delenv(name, raising=False)
    cfg = importlib.reload(config)
    assert cfg.REQUEST_BODY_MAX_BYTES == cfg.DEFAULT_REQUEST_BODY_MAX_BYTES == 1024 * 1024
    assert cfg.API_DOCS_ENABLED is False


def test_a_body_ceiling_inside_its_bounds_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_REQUEST_BODY_MAX_BYTES", "262144")
    assert importlib.reload(config).REQUEST_BODY_MAX_BYTES == 262_144


@pytest.mark.parametrize("raw", ["0", "-1", "65535", str(32 * 1024 * 1024 + 1), "1m"])
def test_a_body_ceiling_outside_its_bounds_is_refused_on_reload(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("RAG_REQUEST_BODY_MAX_BYTES", raw)
    with pytest.raises(ValueError, match="RAG_REQUEST_BODY_MAX_BYTES" if raw != "1m" else "invalid literal"):
        importlib.reload(config)


def test_the_docs_setting_turns_them_on_off_cloud_run_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("RAG_API_DOCS_ENABLED", "1")
    assert importlib.reload(config).API_DOCS_ENABLED is True
    monkeypatch.setenv("K_SERVICE", "game-guide-ai")
    assert importlib.reload(config).API_DOCS_ENABLED is False
