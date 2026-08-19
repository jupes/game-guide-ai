"""`scripts/verify_deploy.py` — proving a deploy landed (x5bz.1.7).

The script exists because `gcloud run deploy` exiting 0 does not mean the code you
built is the code serving. These tests are offline: the evaluation is pure over two
parsed payloads, so every failure mode is reachable from a fixture without a Cloud
Run project.

The fixtures matter as much as the assertions. A `services describe` payload cannot
carry a serving revision's image — that lives on the revision — so the pair here is
(service, revision), and a test that tried to express "right revision, wrong image"
from the service payload alone would be describing a state Cloud Run cannot produce.

Run from repo root:
    uv run --with pytest python -m pytest tests/test_verify_deploy.py -q
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_deploy.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_deploy", MODULE_PATH)
    assert spec and spec.loader, f"cannot load {MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = _load()

IMAGE = "us-central1-docker.pkg.dev/p/r/game-guide-ai:d9ce257abc"
OLD_IMAGE = "us-central1-docker.pkg.dev/p/r/game-guide-ai:610cfcadef"


def service(
    *, created: str = "svc-00002-prj", ready: str = "svc-00002-prj",
    traffic: list[dict[str, Any]] | None = None,
    generation: int = 2, observed: int | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {"generation": generation},
        "status": {
            "observedGeneration": generation if observed is None else observed,
            "latestCreatedRevisionName": created,
            "latestReadyRevisionName": ready,
            "traffic": (
                [{"revisionName": ready, "percent": 100}] if traffic is None else traffic
            ),
        },
    }


def revision(image: str = IMAGE) -> dict[str, Any]:
    return {"spec": {"containers": [{"image": image}]}}


# ── The one state that should pass ───────────────────────────────────────────


def test_the_expected_image_serving_all_traffic_verifies_clean() -> None:
    assert V.evaluate(service(), revision(), IMAGE) == []


# ── The failure this script was written for ──────────────────────────────────


def test_a_revision_that_never_became_ready_is_caught() -> None:
    """2026-08-09, in fixture form: a new revision is created, fails to start, and
    Cloud Run keeps the previous one serving while `gcloud run deploy` exits 0."""
    stale = service(created="svc-00003-xyz", ready="svc-00002-prj")
    failures = V.evaluate(stale, revision(OLD_IMAGE), IMAGE)

    assert failures, "a revision that never became ready must not verify"
    assert any("never became ready" in f for f in failures)
    assert any("svc-00003-xyz" in f and "svc-00002-prj" in f for f in failures), (
        "the message must name both revisions — which was built and which serves"
    )


def test_the_right_revision_running_the_wrong_image_is_caught() -> None:
    """The silent no-op: everything about the service payload looks correct — the
    latest revision is ready and holds all traffic — and it is running yesterday's
    image. Only the revision payload can see this, which is why there are two."""
    failures = V.evaluate(service(), revision(OLD_IMAGE), IMAGE)

    assert any(OLD_IMAGE in f and IMAGE in f for f in failures), (
        "the message must show what is serving AND what was expected"
    )


def test_split_traffic_does_not_count_as_deployed() -> None:
    """Mid-rollout, no revision is *the* deployed one. Answering with the majority
    would let a half-finished rollout report success."""
    split = service(traffic=[
        {"revisionName": "svc-00002-prj", "percent": 90},
        {"revisionName": "svc-00001-fcx", "percent": 10},
    ])
    assert any("100%" in f or "split" in f for f in V.evaluate(split, revision(), IMAGE))


def test_traffic_pinned_to_an_older_revision_is_caught() -> None:
    """A deliberate rollback leaves the newest revision ready but not serving."""
    pinned = service(traffic=[{"revisionName": "svc-00001-fcx", "percent": 100}])
    assert any("svc-00001-fcx" in f for f in V.evaluate(pinned, revision(), IMAGE))


# ── Fail closed, never vacuously ─────────────────────────────────────────────


def test_a_stale_status_is_refused_rather_than_believed() -> None:
    """Well-formed but read back before the controller reconciled: it describes the
    PREVIOUS rollout. Every other field would agree with itself and be wrong."""
    lagging = service(generation=3, observed=2)
    assert any("stale" in f for f in V.evaluate(lagging, revision(), IMAGE))


def test_an_empty_payload_fails_instead_of_passing() -> None:
    """What a failed or unauthenticated gcloud call produces. The dangerous outcome
    is not an error — it is reporting success because nothing contradicted us."""
    failures = V.evaluate({}, {}, IMAGE)
    assert len(failures) >= 3, f"an empty payload must fail loudly, got {failures}"


def test_a_payload_missing_the_container_image_fails() -> None:
    assert any("image" in f for f in V.evaluate(service(), {"spec": {}}, IMAGE))


# ── serving_revision, directly ───────────────────────────────────────────────


def test_serving_revision_finds_the_sole_full_traffic_target() -> None:
    assert V.serving_revision(service()) == "svc-00002-prj"


def test_serving_revision_is_none_when_nothing_holds_all_traffic() -> None:
    split = service(traffic=[
        {"revisionName": "a", "percent": 50}, {"revisionName": "b", "percent": 50},
    ])
    assert V.serving_revision(split) is None
    assert V.serving_revision({}) is None


# ── Exit codes: CI reads these, not the prose ────────────────────────────────


def test_main_refuses_a_wrong_argument_count() -> None:
    assert V.main(["verify_deploy.py"]) == 2


def test_evaluate_reports_every_failure_not_just_the_first() -> None:
    """During an incident, one-at-a-time diagnostics cost a round trip each."""
    broken = service(created="new", ready="old", generation=3, observed=2)
    assert len(V.evaluate(broken, revision(OLD_IMAGE), IMAGE)) >= 3
