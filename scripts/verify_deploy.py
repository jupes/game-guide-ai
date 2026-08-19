#!/usr/bin/env python3
"""Verify that a deploy actually landed (x5bz.1.7).

`gcloud run deploy` returning 0 does not mean the code you built is the code now
serving. A revision can be created and never become Ready, in which case Cloud Run
keeps the previous one at 100% and the deploy command still succeeds. On
2026-08-09 a green deploy job was read as "auth is live"; the running revision was
the previous build, and ingress was opened onto it.

So this asks one question — **is the image we just pushed the image now serving
100% of traffic?** — and answers it from the platform, not from an exit code.

Two payloads are required, and that is the whole subtlety. The image is NOT in the
service payload: `spec.template` is the Configuration for the *next* revision, and
`status.traffic[]` carries only revision names. In the exact failure this exists to
catch, `spec.template` already holds the new image while an older revision serves —
so reading it there would report success. The serving revision is resolved from
traffic, then its own image is read from a second call. `verify_auth_throttle.py`
documents the same trap for a different field.

Usage:
    python scripts/verify_deploy.py <service> <region> <project> <expected-image> [<expected-digest>]

Exit 0 = the expected image is serving all traffic. Exit 1 = anything else, with
every failed condition listed rather than just the first.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

#: Cloud Run splits traffic in whole percent, so this is exact, not a tolerance.
ALL_TRAFFIC = 100


def _dig(payload: Any, *path: str) -> Any:
    """Walk nested dicts, returning None rather than raising on a missing key.

    Every field below is read this way: a payload that changed shape must produce
    a reported failure, never a KeyError that reads like a broken script.
    """
    for key in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def serving_revision(service: dict[str, Any]) -> str | None:
    """The revision taking ALL traffic, or None if traffic is split or absent.

    Split traffic is deliberately not "the biggest share": during a partial
    rollout no single revision is *the* deployed one, and answering with the
    majority would let a half-finished rollout report success.
    """
    targets = _dig(service, "status", "traffic")
    if not isinstance(targets, list):
        return None
    full = [
        t.get("revisionName")
        for t in targets
        if isinstance(t, dict) and t.get("percent") == ALL_TRAFFIC
    ]
    return full[0] if len(full) == 1 and isinstance(full[0], str) else None


def image_matches(serving: str, expected_image: str, expected_digest: str) -> bool:
    """Does the serving image refer to what we pushed?

    Two spellings of the same thing. `docker push` sends a TAG; Cloud Run
    RESOLVES that tag to a digest when it creates the revision, so the revision
    reports `repo/name@sha256:...` and a tag-to-tag comparison can never match.
    That is not hypothetical — it failed the first real merge (run 32309272588)
    on a deploy that had in fact succeeded.

    Matching either spelling keeps the check honest in both directions: the
    digest is the strong form (right bytes, not just the right label), and the
    tag remains valid for any path that reports one.
    """
    return serving == expected_image or (bool(expected_digest) and serving == expected_digest)


def evaluate(
    service: dict[str, Any], revision: dict[str, Any], expected_image: str,
    expected_digest: str = "",
) -> list[str]:
    """Every reason this deploy has not landed. Empty list means it has.

    All four conditions are reported together: told only the first, an operator
    fixes it, re-runs, and meets the next one — during an incident, that is three
    round trips instead of one.
    """
    failures: list[str] = []

    # Staleness first. Everything below reads `status`, and a status read back
    # before the control plane reconciles describes the PREVIOUS rollout while
    # being perfectly well-formed — internally consistent and wrong.
    generation = _dig(service, "metadata", "generation")
    observed = _dig(service, "status", "observedGeneration")
    if generation is None or observed is None:
        failures.append(
            f"cannot confirm the status is current "
            f"(metadata.generation={generation!r}, status.observedGeneration={observed!r})"
        )
    elif generation != observed:
        failures.append(
            f"status is stale: generation {generation} but the controller has "
            f"only observed {observed} — it describes the previous rollout"
        )

    created = _dig(service, "status", "latestCreatedRevisionName")
    ready = _dig(service, "status", "latestReadyRevisionName")
    if created is None or ready is None:
        failures.append(
            f"cannot read the latest revisions "
            f"(created={created!r}, ready={ready!r})"
        )
    elif created != ready:
        failures.append(
            f"the new revision never became ready: created {created}, but the "
            f"latest ready one is {ready} — the old revision is still serving"
        )

    serving = serving_revision(service)
    if serving is None:
        failures.append(
            "no single revision holds 100% of traffic — the rollout is split or "
            "traffic could not be read"
        )
    elif ready is not None and serving != ready:
        failures.append(
            f"traffic is on {serving}, not the latest ready revision {ready}"
        )

    containers = _dig(revision, "spec", "containers")
    image = (
        containers[0].get("image")
        if isinstance(containers, list) and containers and isinstance(containers[0], dict)
        else None
    )
    if image is None:
        failures.append("could not read the serving revision's container image")
    elif not image_matches(image, expected_image, expected_digest):
        # Both forms in the message: a reader has to be able to tell a genuine
        # mismatch from the two-spellings-of-one-image case at a glance.
        wanted = f"{expected_image} ({expected_digest})" if expected_digest else expected_image
        failures.append(
            f"the serving revision runs {image}, not the image just built "
            f"({wanted}) — the deploy did not take effect"
        )

    return failures


def _gcloud_json(*args: str) -> dict[str, Any]:
    """Run a gcloud command expecting JSON. Any failure is an empty payload, which
    `evaluate` then reports as unreadable rather than crashing."""
    try:
        out = subprocess.run(
            ["gcloud", *args, "--format=json"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
        parsed = json.loads(out)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        print(f"  gcloud {' '.join(args)} failed: {exc}", file=sys.stderr)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main(argv: list[str]) -> int:
    if len(argv) not in (5, 6):
        print(__doc__)
        return 2
    service_name, region, project, expected_image = argv[1:5]
    expected_digest = argv[5] if len(argv) == 6 else ""

    service = _gcloud_json(
        "run", "services", "describe", service_name,
        f"--region={region}", f"--project={project}",
    )
    serving = serving_revision(service)
    revision: dict[str, Any] = {}
    if serving is not None:
        revision = _gcloud_json(
            "run", "revisions", "describe", serving,
            f"--region={region}", f"--project={project}",
        )

    failures = evaluate(service, revision, expected_image, expected_digest)
    if failures:
        print(f"DEPLOY NOT VERIFIED — {service_name} is not serving {expected_image}")
        for failure in failures:
            print(f"  FAIL  {failure}")
        print(
            "\nThe deploy step exiting 0 is not evidence on its own; this is why.\n"
            "Roll back with: gcloud run services update-traffic "
            f"{service_name} --region={region} --to-revisions=<known-good>=100"
        )
        return 1

    print(f"OK — {serving} is serving 100% of traffic and runs {expected_image}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
