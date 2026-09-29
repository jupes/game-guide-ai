"""The canary harness's demonstration (agent-forge-harness-1ir.1.10): a leak test that fails exactly
when a canary is deliberately routed into a player-path capture or a log record.

This module is **not collected** by a normal run: its name does not match ``test_*.py``.
``test_canary_demo.py`` calls ``run_demo`` in process and also runs this file under a fresh pytest
in a subprocess, asserting each outcome from the JUnit report. To watch it fail by hand:

    uv run --frozen --no-sync python -m pytest -q service/tests/canary_demo_flow.py

Expected: ``test_demonstration[none]`` passes; ``[player_capture]`` and ``[log]`` fail with
``CanaryLeak``; ``test_forgot_to_assert`` errors at teardown with ``CaptureNotAsserted``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from service.generate import LLMClient
from service.tests.canary import Audience, CanaryDocument, CanaryWorld, LeakCapture

ROUTES = ("none", "player_capture", "log")
_log = logging.getLogger("service.tests.canary_demo")


def compose_player_card(doc: CanaryDocument, mask: Sequence[str], llm: LLMClient, *, route: str) -> str:
    """A stand-in for a player-path composer (no product one exists yet)."""
    shown = {key: doc.data[key] for key in mask}
    if route == "player_capture":
        shown["aside"] = doc.data["true_identity"]  # the deliberate leak: a field the player may not see
    reply = llm.invoke([
        SystemMessage(content="Write a table card from these fields only."),
        HumanMessage(content=json.dumps(shown, sort_keys=True)),
    ])
    _log.info("player card composed (fields=%d)", len(shown))
    if route == "log":
        _log.info("composed for %s", doc.data["true_identity"])  # the deliberate leak into a log record
    return str(reply.content)


def run_demo(world: CanaryWorld, capture: LeakCapture, route: str) -> None:
    """Compose one NPC's table card through a recording LLM and a player slot, then assert clean."""
    if route not in ROUTES:
        raise ValueError(f"route is one of {ROUTES}")
    npc = world.document("npc", campaign="A")
    llm = capture.llm("demo-player-llm", audience=Audience.PLAYER,
                      replies=lambda messages: f"card: {npc.data['name']}")
    slot = capture.channel("demo-player-slot", audience=Audience.PLAYER)
    slot.publish(compose_player_card(npc, ["name", "voice"], llm, route=route).encode(), topic="slot-table")
    visible = npc.field("name") | npc.field("voice")
    capture.assert_clean(
        visible={"demo-player-llm": visible, "demo-player-slot": visible},
        must_see={"demo-player-llm": npc.field("name"), "demo-player-slot": npc.field("name")},
    )


@pytest.mark.parametrize("route", ROUTES)
def test_demonstration(route: str, leak_capture: LeakCapture, canary_world: CanaryWorld) -> None:
    run_demo(canary_world, leak_capture, route)


def test_forgot_to_assert(leak_capture: LeakCapture) -> None:
    leak_capture.llm("unasserted", audience=Audience.PLAYER)  # errors at teardown: CaptureNotAsserted
