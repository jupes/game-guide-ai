"""
Contract for `scripts/verify-auth-throttle.sh` (x5bz.2, security review round 10).

That script is the only thing standing between a misconfigured
`AUTH_TRUSTED_PROXY_HOPS` and a public deployment whose login rate limiter keys
on attacker-supplied text. It is run by hand, once, against production — so its
*decision logic* has to be right the first time, and the failure mode that
matters is a **false PASS**.

The specific trap: Cloud Run returns **429** of its own when no container
instance is available (see its troubleshooting docs), which is indistinguishable
by status code from our limiter firing. So the app marks its own throttle
responses with a header, and the script requires that marker.

These tests run the real script against a stub HTTP service that replays each
scenario, and assert on its exit status:

    0 PASS   1 FAIL   2 REFUSE (under-sized probe)   3 ABORT (inconclusive)

Run from repo root:
    uv run --with pytest python -m pytest tests/test_throttle_verifier.py -q
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from _bash import bash_or_skip

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify-auth-throttle.sh"
APP_PY = REPO_ROOT / "service" / "app.py"

PASS, FAIL, REFUSE, ABORT = 0, 1, 2, 3


def _handler_for(script: list[tuple[int, dict[str, str]]], tail: tuple[int, dict[str, str]]):
    """Reply with `script` in order, then `tail` forever."""
    state = {"n": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            with lock:
                i = state["n"]
                state["n"] += 1
            status, headers = script[i] if i < len(script) else tail
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass  # keep pytest output clean

    return Handler


@pytest.fixture
def stub():
    """Start a stub auth service; yields a factory returning its base URL."""
    servers: list[ThreadingHTTPServer] = []

    def start(script, tail):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(script, tail))
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _run(bash: str, base: str, **env_overrides) -> subprocess.CompletedProcess:
    env = {
        "PATH": __import__("os").environ["PATH"],
        # State the topology so the script never reaches for gcloud.
        "PER_SOURCE": "3",
        "CAPACITY": "1",
        "ATTEMPTS": "10",
        **env_overrides,
    }
    return subprocess.run(
        [bash, str(SCRIPT), base],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT, env=env,
    )


@pytest.fixture(scope="module")
def bash():
    if shutil.which("curl") is None:
        pytest.skip("curl unavailable; the verifier is a curl script")
    return bash_or_skip()


# ── The header contract between app and script ───────────────────────────────


def test_app_and_script_agree_on_the_marker() -> None:
    """The script matches a literal header name and value. If the app renames or
    revalues it, the script silently stops finding it and every real throttle
    becomes an ABORT — or worse, someone 'fixes' that by dropping the check."""
    app = APP_PY.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'AUTH_THROTTLE_HEADER = "X-Auth-Throttled"' in app
    assert 'AUTH_THROTTLE_HEADER: "1"' in app
    assert 'readonly MARKER_NAME="x-auth-throttled"' in script
    assert 'readonly MARKER_VALUE="1"' in script


def test_the_marker_contract_is_not_environment_configurable() -> None:
    """It was briefly `MARKER="${MARKER:-...}"` for test convenience, which handed
    the whole vulnerability back: ambient state could nominate any header — say
    `Server`, which every platform 429 carries — as proof our limiter fired.
    What counts as trustworthy is a contract, not a knob."""
    script = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r'MARKER\w*="\$\{', code), (
        "the marker must not be defaulted in from the environment"
    )
    for name in ("MARKER_NAME", "MARKER_VALUE"):
        assert f'readonly {name}=' in code, f"{name} must be readonly"


# ── Verdicts ─────────────────────────────────────────────────────────────────


def test_marked_429_is_the_only_thing_that_passes(bash, stub) -> None:
    base = stub([(401, {})] * 3, (429, {"X-Auth-Throttled": "1", "Retry-After": "60"}))
    result = _run(bash, base)
    assert result.returncode == PASS, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_unmarked_429_aborts_instead_of_passing(bash, stub) -> None:
    """The reported hole: Cloud Run sheds load with a bare 429 when no instance
    is available. Accepting it would certify the proxy configuration on the
    strength of a transient platform response."""
    base = stub([(401, {})] * 3, (429, {"Retry-After": "5"}))
    result = _run(bash, base)
    assert result.returncode == ABORT, result.stdout + result.stderr
    assert "PASS" not in result.stdout
    assert "x-auth-throttled: 1" in result.stdout  # says what was missing
    assert "INCONCLUSIVE" in result.stdout


def test_marker_env_override_cannot_manufacture_a_pass(bash, stub) -> None:
    """The reported reproduction: a platform 429 carrying an ordinary `Server`
    header, verified with `MARKER=server`. If the environment can nominate the
    trusted header, the check certifies whatever it is told to."""
    base = stub([(401, {})] * 3, (429, {"Server": "Google Frontend"}))
    result = _run(bash, base, MARKER="server")
    assert result.returncode == ABORT, result.stdout + result.stderr
    assert "PASS" not in result.stdout


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Auth-Throttled": ""},          # present but empty
        {"X-Auth-Throttled": "0"},         # present, wrong value
        {"X-Auth-Throttled": "yes"},
        {"X-Auth-Throttled": "1 1"},
        {"X-Auth-Throttled-Extra": "1"},   # different field name
        {"Not-X-Auth-Throttled": "1"},     # must anchor at line start
    ],
)
def test_presence_alone_does_not_pass(bash, stub, headers) -> None:
    """A header that merely shares the name isn't our limiter saying "budget
    exhausted" — the value carries the meaning."""
    base = stub([(401, {})] * 3, (429, headers))
    result = _run(bash, base)
    assert result.returncode == ABORT, result.stdout + result.stderr
    assert "PASS" not in result.stdout


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Auth-Throttled": "1"},
        {"x-auth-throttled": "1"},         # field names are case-insensitive
        {"X-AUTH-THROTTLED": "1"},
        {"X-Auth-Throttled": " 1 "},       # optional surrounding whitespace
    ],
)
def test_the_real_marker_passes_however_it_is_cased(bash, stub, headers) -> None:
    base = stub([(401, {})] * 3, (429, headers))
    result = _run(bash, base)
    assert result.returncode == PASS, result.stdout + result.stderr


def test_all_401s_is_a_real_failure(bash, stub) -> None:
    """Budget exhausted with no throttle: every spoofed address bought its own."""
    base = stub([], (401, {}))
    result = _run(bash, base)
    assert result.returncode == FAIL, result.stdout + result.stderr
    assert "FAIL" in result.stdout


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (403, "IAM"),        # never reached the app
        (422, "payload"),    # request shape drifted
        (500, "erroring"),
        (503, "failing closed"),
    ],
)
def test_unexpected_statuses_abort_with_a_diagnostic(bash, stub, status, hint) -> None:
    base = stub([], (status, {}))
    result = _run(bash, base)
    assert result.returncode == ABORT, result.stdout + result.stderr
    assert "PASS" not in result.stdout
    assert str(status) in result.stdout
    assert hint in result.stdout


def test_a_marked_429_after_unexpected_traffic_still_aborts(bash, stub) -> None:
    """An abort must not be recoverable by a later good response — the run is
    inconclusive from the moment something unexplained happened."""
    base = stub([(401, {}), (503, {})], (429, {"X-Auth-Throttled": "1"}))
    result = _run(bash, base)
    assert result.returncode == ABORT, result.stdout + result.stderr


def test_an_undersized_probe_refuses_before_sending_anything(bash, stub) -> None:
    """A FAIL that cannot be trusted must not be produced at all."""
    base = stub([], (401, {}))
    result = _run(bash, base, PER_SOURCE="30", CAPACITY="2", ATTEMPTS="40")
    assert result.returncode == REFUSE, result.stdout + result.stderr
    assert "REFUSING" in result.stdout


def test_an_unreachable_service_aborts(bash) -> None:
    result = _run(bash, "http://127.0.0.1:1")  # nothing listening
    assert result.returncode == ABORT, result.stdout + result.stderr
    assert "PASS" not in result.stdout
