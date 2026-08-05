#!/usr/bin/env python3
"""Verify, against a DEPLOYED service, that auth throttling keys on something
the caller cannot choose.

Behavioural because nothing else distinguishes the cases: a request log shows
what Google observed, not what `client_source()` picked out of the
X-Forwarded-For chain. So send attempts differing ONLY in a spoofed
X-Forwarded-For (emails rotate too, so the ACCOUNT limiter cannot be what trips)
and see whether they share a budget.

    PASS   (0)  a 429 carrying the app's marker — spoofed values shared a budget
    FAIL   (1)  only 401s: each spoofed value bought its own budget
    REFUSE (2)  the attempts cannot exhaust the budget; nothing is sent
    ABORT  (3)  anything else — INCONCLUSIVE, never a pass

Cost: spends the source budget for the address the service really sees, so your
own sign-ins get 429 for AUTH_RATE_LIMIT_WINDOW_S. It decays on its own.

Usage:
    python scripts/verify_auth_throttle.py https://<service-url>
    ATTEMPTS=200 PER_SOURCE=30 CAPACITY=4 python scripts/verify_auth_throttle.py <url>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from enum import Enum

# INVARIANT: the marker is this check's contract with the app, never read from
# the environment. Cloud Run returns 429 itself when no instance is available,
# so a bare status code cannot tell a platform hiccup from our limiter, and
# ambient state must not get to decide what counts as proof. The app sets this
# on every 429 its auth limiter raises and on nothing else (AUTH_THROTTLE_HEADER
# in service/app.py).
MARKER_NAME = "x-auth-throttled"
MARKER_VALUE = "1"

EXIT_PASS, EXIT_FAIL, EXIT_REFUSE, EXIT_ABORT = 0, 1, 2, 3

MAX_SCALE_ANNOTATION = "autoscaling.knative.dev/maxScale"


class Verdict(Enum):
    """What one probe response means for the run."""

    CONTINUE = "continue"     # expected 401 — keep spending the budget
    THROTTLED = "throttled"   # our limiter fired: PASS
    ABORT = "abort"           # inconclusive; never a pass


# ── Pure classification (the part worth testing directly) ────────────────────


def has_app_marker(headers: Iterable[tuple[str, str]] | Mapping[str, str]) -> bool:
    """True only for exactly `X-Auth-Throttled: 1`.

    Presence is not enough — an empty or unexpected value is something else
    sharing a header name, not our limiter reporting an exhausted budget. Field
    names are case-insensitive (RFC 9110) and values may carry surrounding
    whitespace, so both are normalised first.
    """
    items = headers.items() if isinstance(headers, Mapping) else headers
    return any(
        name.strip().lower() == MARKER_NAME and value.strip().lower() == MARKER_VALUE
        for name, value in items
    )


def classify(
    status: int, headers: Iterable[tuple[str, str]] | Mapping[str, str],
) -> tuple[Verdict, str]:
    """Map one response to a verdict and an operator-facing explanation."""
    if status == 401:
        return Verdict.CONTINUE, ""
    if status == 429:
        if has_app_marker(headers):
            return Verdict.THROTTLED, ""
        return Verdict.ABORT, (
            f"429 without '{MARKER_NAME}: {MARKER_VALUE}'. That is not our rate "
            "limiter: Cloud Run returns 429 of its own when no instance is "
            "available, and treating it as a pass would certify the proxy "
            "configuration on a platform hiccup. Wait for the service to settle "
            "(check instance count / cold starts) and re-run."
        )
    hint = {
        403: "Cloud Run IAM is still locked — this probe never reached the app.",
        422: "the login payload was rejected; the request shape has changed.",
        0: "no HTTP response (DNS, TLS or connection failure).",
        503: "the app is failing closed — likely SESSION_SECRET or the DB.",
    }.get(status) or ("the service is erroring; fix that first." if status >= 500 else "")
    return Verdict.ABORT, f"unexpected HTTP {status}; expected 401. {hint}".strip()


def total_capacity(
    traffic: Iterable[Mapping[str, object]], max_scale: Mapping[str, str | None],
) -> int | None:
    """Instance slots = sum of each TRAFFIC-SERVING revision's own maxScale.

    Per revision, not from the current template: an older revision still taking
    traffic keeps the limit it was deployed with. None when any serving revision
    has no maxScale — unbounded autoscaling has no budget to derive, and
    guessing under-counts, which reports a false FAILURE.
    """
    total = 0
    for entry in traffic:
        name = entry.get("revisionName")
        percent = entry.get("percent") or 0
        if not name or not isinstance(percent, int) or percent <= 0:
            continue
        limit = max_scale.get(str(name))
        if not limit:
            return None
        total += int(limit)
    return total or None


def default_attempts(per_source: int, capacity: int) -> int:
    """Budget plus a margin, so an uneven split across instances still trips it.

    The limiter is per PROCESS and requests spread over every serving container,
    so fewer than `per_source × capacity` attempts reports a false FAILURE — with
    the shipped settings a 40-attempt probe split 20/20 across two instances
    returns forty 401s from a perfectly configured deployment.
    """
    return per_source * capacity + per_source


# ── I/O ──────────────────────────────────────────────────────────────────────


def _gcloud_json(*args: str) -> object | None:
    try:
        out = subprocess.run(
            ["gcloud", *args, "--format=json"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def derive_capacity(service: str, region: str) -> int | None:
    described = _gcloud_json("run", "services", "describe", service, "--region", region)
    if not isinstance(described, dict):
        return None
    traffic = described.get("status", {}).get("traffic", [])
    if not isinstance(traffic, list):
        return None
    scales: dict[str, str | None] = {}
    for entry in traffic:
        name = entry.get("revisionName") if isinstance(entry, dict) else None
        if not name or name in scales:
            continue
        rev = _gcloud_json("run", "revisions", "describe", str(name), "--region", region)
        annotations = (
            rev.get("metadata", {}).get("annotations", {}) if isinstance(rev, dict) else {}
        )
        scales[str(name)] = annotations.get(MAX_SCALE_ANNOTATION)
    return total_capacity(traffic, scales)


def probe(base: str, attempt: int) -> tuple[int, list[tuple[str, str]]]:
    """One login attempt with a spoofed source address. Returns (status, headers);
    status 0 means no HTTP response at all."""
    body = json.dumps({
        "email": f"throttle-probe-{attempt}@example.invalid",
        "password": "not-a-real-password",
    }).encode()
    request = urllib.request.Request(  # noqa: S310 - operator-supplied https URL
        f"{base}/auth/login", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-For": f"198.51.100.{(attempt % 250) + 1}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, list(response.headers.items())
    except urllib.error.HTTPError as exc:  # a response, just not a 2xx
        return exc.code, list(exc.headers.items())
    except (urllib.error.URLError, OSError, ValueError):
        return 0, []


PASS_NEXT_STEPS = """
Now confirm the key is the RIGHT one (not everyone collapsed into a single
bucket, which also passes the test above). The throttle entry carries the
derived key and a trace; join it to the request log entry, which is where the
address Google observed lives:

  gcloud logging read 'jsonPayload.message="auth attempt throttled"' \\
    --limit 1 --format='value(jsonPayload.source, trace)'
  gcloud logging read 'logName:"run.googleapis.com%2Frequests" AND trace="<TRACE>"' \\
    --limit 1 --format='value(httpRequest.remoteIp)'

  The two addresses must match. If every tester's entries show the SAME
  jsonPayload.source, hops is too low. See docs/deploy-gcp.md §9.
"""


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].startswith("http"):
        print("usage: verify_auth_throttle.py <service-url>", file=sys.stderr)
        return EXIT_REFUSE
    base = argv[1].rstrip("/")

    per_source = int(os.environ.get("PER_SOURCE", "30"))  # AUTH_RATE_LIMIT_PER_SOURCE
    capacity = int(os.environ["CAPACITY"]) if os.environ.get("CAPACITY") else None
    if capacity is None:
        capacity = derive_capacity(
            os.environ.get("SERVICE", "game-guide-ai"),
            os.environ.get("GCP_REGION", "us-central1"),
        )
        if capacity is None:
            capacity = 2
            print(
                f"note: could not read the live traffic split / maxScale (unbounded\n"
                f"      autoscaling, or gcloud unavailable). Assuming {capacity} instance\n"
                f"      slots — set CAPACITY explicitly before trusting a FAIL result."
            )

    budget = per_source * capacity
    attempts = int(os.environ.get("ATTEMPTS") or default_attempts(per_source, capacity))

    print(f"Rate-limit budget to exhaust: {per_source} per source × {capacity} "
          f"instance slot(s) = {budget}")
    print(f"Probing {base}/auth/login with up to {attempts} attempts, rotating BOTH "
          f"the spoofed X-Forwarded-For and the email...")

    if attempts <= budget:
        print(f"REFUSING: {attempts} attempts cannot exhaust a budget of {budget} — this\n"
              f"          would report a false failure. Raise ATTEMPTS above {budget}.")
        return EXIT_REFUSE

    for attempt in range(1, attempts + 1):
        status, headers = probe(base, attempt)
        print(status, end=" ", flush=True)
        verdict, reason = classify(status, headers)
        if verdict is Verdict.THROTTLED:
            print(f"\n\nPASS — spoofed X-Forwarded-For values shared one budget, so the\n"
                  f"       source key is not caller-controlled.\n{PASS_NEXT_STEPS}")
            return EXIT_PASS
        if verdict is Verdict.ABORT:
            print(f"\n\nABORT on attempt {attempt} — {reason}\n"
                  f"        Result is INCONCLUSIVE, not a pass.", file=sys.stderr)
            return EXIT_ABORT

    print(f"\n\nFAIL — {attempts} attempts from rotating spoofed addresses, no 429,\n"
          f"       against a budget of {budget}. Each spoofed value bought a fresh\n"
          f"       budget. Check that AUTH_TRUSTED_PROXY_HOPS matches the real proxy\n"
          f"       chain (1 for the run.app front end, 2 behind an external HTTPS load\n"
          f"       balancer) and that ingress cannot be bypassed. See docs/deploy-gcp.md §9.")
    return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main(sys.argv))
