"""`scripts/verify_auth_throttle.py` — the deployed throttle check.

Its job is to be un-foolable: the only thing that may be reported as a pass is a
429 carrying the application's own marker. Cloud Run returns 429 itself when no
instance is available, so accepting a bare status code would certify a broken
proxy configuration on the strength of a platform hiccup.

The classification is a pure function, so these tests call it directly rather
than standing up an HTTP server to reach it.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_throttle_verifier.py -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_auth_throttle.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_auth_throttle", MODULE_PATH)
    assert spec and spec.loader, f"cannot load {MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = _load()
MARKER = [("X-Auth-Throttled", "1")]


# ── Only the app's own 429 is a pass ─────────────────────────────────────────


def test_a_marked_429_is_the_only_pass():
    assert V.classify(429, MARKER)[0] is V.Verdict.THROTTLED


def test_an_unmarked_429_aborts_rather_than_passing():
    """The platform 429. Reporting it as a pass is the failure mode this whole
    marker requirement exists to prevent."""
    verdict, reason = V.classify(429, [("Server", "Google Frontend")])
    assert verdict is V.Verdict.ABORT
    assert "Cloud Run returns 429" in reason


@pytest.mark.parametrize("headers", [
    [("X-Auth-Throttled", "")],                        # present but empty
    [("X-Auth-Throttled", "0")],                       # wrong value
    [("X-Auth-Throttled", "yes")],
    [("X-Auth-Throttled", "1 1")],
    [("X-Auth-Throttled-Extra", "1")],                 # near-miss name
    [("Not-X-Auth-Throttled", "1")],
    [("Server", "1")],                                 # right value, wrong field
])
def test_near_miss_headers_do_not_count_as_the_marker(headers):
    assert not V.has_app_marker(headers)
    assert V.classify(429, headers)[0] is V.Verdict.ABORT


@pytest.mark.parametrize("headers", [
    [("x-auth-throttled", "1")],                       # RFC 9110: names fold case
    [("X-AUTH-THROTTLED", "1")],
    [("X-Auth-Throttled", " 1 ")],                     # legal surrounding space
    [("X-Auth-Throttled", "1\r")],                     # CRLF line endings
    [("Server", "nginx"), ("X-Auth-Throttled", "1")],  # not the first header
])
def test_the_real_marker_is_recognised_however_it_arrives(headers):
    assert V.has_app_marker(headers)


def test_the_marker_is_a_constant_not_an_environment_override(monkeypatch):
    """It was briefly overridable for test convenience, which handed the whole
    vulnerability back: an override of `server` made an ordinary Server header on
    a platform 429 read as proof our limiter fired."""
    for var in ("MARKER", "MARKER_NAME", "MARKER_VALUE"):
        monkeypatch.setenv(var, "server")
    reloaded = _load()
    assert reloaded.MARKER_NAME == "x-auth-throttled"
    assert reloaded.MARKER_VALUE == "1"
    assert not reloaded.has_app_marker([("Server", "Google Frontend")])


# ── Everything else is inconclusive, never a pass ────────────────────────────


def test_401_continues_spending_the_budget():
    assert V.classify(401, [])[0] is V.Verdict.CONTINUE


@pytest.mark.parametrize("status", [0, 200, 403, 422, 500, 502, 503])
def test_other_statuses_abort(status):
    verdict, reason = V.classify(status, [])
    assert verdict is V.Verdict.ABORT
    assert reason, f"HTTP {status} must explain itself to the operator"


@pytest.mark.parametrize(("status", "expected"), [
    (403, "IAM"), (422, "payload"), (503, "failing closed"), (0, "no HTTP response"),
])
def test_the_common_failures_name_their_likely_cause(status, expected):
    assert expected in V.classify(status, [])[1]


# ── Budget arithmetic: under-counting produces a false FAILURE ───────────────


def test_capacity_sums_each_serving_revisions_own_max_scale():
    """Revisions do not share the current template's maxScale — an older one
    still taking traffic keeps what it was deployed with."""
    traffic = [{"revisionName": "rev-a", "percent": 50},
               {"revisionName": "rev-b", "percent": 50}]
    assert V.total_capacity(traffic, {"rev-a": "2", "rev-b": "5"}) == 7


def test_capacity_ignores_revisions_serving_no_traffic():
    traffic = [{"revisionName": "live", "percent": 100},
               {"revisionName": "old", "percent": 0}]
    assert V.total_capacity(traffic, {"live": "3", "old": "99"}) == 3


def test_unbounded_autoscaling_has_no_derivable_budget():
    """No maxScale means no ceiling to exhaust; guessing would report a false
    FAILURE, so the caller must state CAPACITY."""
    traffic = [{"revisionName": "rev-a", "percent": 100}]
    assert V.total_capacity(traffic, {"rev-a": None}) is None


def test_default_attempts_exceed_the_budget():
    """The limiter is per process and requests spread across every serving
    container, so sending only the budget reports a false FAILURE."""
    per_source, capacity = 30, 2
    assert V.default_attempts(per_source, capacity) > per_source * capacity


def test_exit_codes_are_the_documented_contract():
    assert (V.EXIT_PASS, V.EXIT_FAIL, V.EXIT_REFUSE, V.EXIT_ABORT) == (0, 1, 2, 3)


def test_refuses_before_sending_anything_when_attempts_cannot_exhaust_the_budget(
    monkeypatch, capsys,
):
    monkeypatch.setenv("PER_SOURCE", "30")
    monkeypatch.setenv("CAPACITY", "2")
    monkeypatch.setenv("ATTEMPTS", "40")
    monkeypatch.setattr(V, "probe", lambda *a: pytest.fail("sent traffic despite refusing"))

    assert V.main(["prog", "https://example.invalid"]) == V.EXIT_REFUSE
    assert "REFUSING" in capsys.readouterr().out


# ── End to end over a stubbed transport ──────────────────────────────────────


def _run(monkeypatch, responses):
    monkeypatch.setenv("PER_SOURCE", "2")
    monkeypatch.setenv("CAPACITY", "1")
    monkeypatch.setenv("ATTEMPTS", "5")
    calls = iter(responses)
    monkeypatch.setattr(V, "probe", lambda base, attempt: next(calls))
    return V.main(["prog", "https://example.invalid"])


def test_pass_when_the_marked_429_arrives(monkeypatch, capsys):
    code = _run(monkeypatch, [(401, []), (401, []), (429, MARKER)])
    assert code == V.EXIT_PASS
    assert "PASS" in capsys.readouterr().out


def test_fail_when_the_budget_is_never_exhausted(monkeypatch, capsys):
    code = _run(monkeypatch, [(401, [])] * 5)
    assert code == V.EXIT_FAIL
    assert "FAIL" in capsys.readouterr().out


def test_abort_stops_immediately_on_an_unmarked_429(monkeypatch):
    """It must not keep probing and later report PASS on a different response."""
    code = _run(monkeypatch, [(401, []), (429, [("Server", "gfe")]), (429, MARKER)])
    assert code == V.EXIT_ABORT
