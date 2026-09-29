"""The reveal service against the twin alone, and what the source must say
(agent-forge-harness-1kg.7.1).

The behaviour both worlds share is `tests/test_reveal_db.py`'s service suite.
What is here is what only the twin can stage cheaply — a store that loses a
deadlock on purpose, a picture that fails after a Stop committed — and the
structural rules a reviewer would otherwise have to re-read the source for:
every production narrowing names its scope and its clock, every production
store gets the fill, and no reveal module reads a default mask. Each scan also
asserts it found the named targets it exists for, so an empty or mis-rooted
scan fails rather than passing.
"""

from __future__ import annotations

import ast
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from service.audit_log import AuditAction, InMemoryAuditLog
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase
from service.document_store import InMemoryDocumentStore
from service.participant_store import InMemoryParticipantStore
from service.reveal_scope import TableAudience
from service.reveal_store import InMemoryRevealStore, RevealBusy, RevealPicture
from service.reveals import (
    DisplayCommand,
    Reveals,
    StopAll,
    check_mask,
    slot_clear_for,
)
from service.table_session_store import InMemoryTableSessionStore, TableSession
from service.workbench_contracts import Author, DocumentTypeId

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "service"
REVEAL_MODULES = ("reveal_scope.py", "reveal_store.py", "reveals.py")
AN_NPC = {"name": "Vashti", "qualifier": "Harbourmistress", "tags": ["harbour"], "voice": "low"}


def _command() -> str:
    return secrets.token_urlsafe(16)


# ── A twin world ─────────────────────────────────────────────────────────────


@dataclass
class Twin:
    db: InMemoryDatabase
    campaigns: InMemoryCampaignStore
    sessions: InMemoryTableSessionStore
    documents: InMemoryDocumentStore
    rows: InMemoryRevealStore
    audit: InMemoryAuditLog
    campaign: str
    session: TableSession
    document: str

    def service(self, rows: InMemoryRevealStore | None = None, *, attempts: int = 3) -> Reveals:
        return Reveals(
            self.db,
            campaigns=self.campaigns,
            sessions=self.sessions,
            reveals=rows or self.rows,
            documents=self.documents,
            audit=self.audit,
            attempts=attempts,
        )

    def epoch(self) -> int:
        with self.db.transaction() as unit:
            found = self.sessions.get(unit, self.session.id)
        assert found is not None
        return found.reveal_epoch

    def shown(self) -> dict[str | None, str | None]:
        with self.db.transaction() as unit:
            return {s.participant_id: s.disclosure_id for s in self.rows._slots(unit, self.session.id)}

    def live(self) -> list[str]:
        with self.db.transaction() as unit:
            return [d.id for d in self.rows.live_disclosures(unit, self.session.id)]

    def actions(self) -> list[str]:
        with self.db.transaction() as unit:
            return [str(e.action) for e in self.audit.for_campaign(unit, self.campaign)]

    def confirm(self, service: Reveals) -> Any:
        return service.display(
            DisplayCommand(
                self.campaign, self.session.id, _command(), self.epoch(), self.document, 1, ("name",), TableAudience()
            ),
            owner_id=1,
            now=datetime.now(UTC),
        )


def _twin(rows_type: type[InMemoryRevealStore] = InMemoryRevealStore, **extra: Any) -> tuple[Twin, Any]:
    db = InMemoryDatabase()
    rows = rows_type(db, **extra) if extra else rows_type(db)
    campaigns = InMemoryCampaignStore(db)
    sessions = InMemoryTableSessionStore(db, slot_clear=slot_clear_for(rows))
    documents = InMemoryDocumentStore(db)
    InMemoryParticipantStore(db)
    now = datetime.now(UTC)
    with db.transaction() as unit:
        campaign = campaigns.create(unit, owner_id=1, name="Nocturne").id
    with db.transaction() as unit:
        session = sessions.start(
            unit, campaign, owner_id=1, expires_at=now + timedelta(hours=12), command_id=_command(), now=now
        )
    with db.transaction() as unit:
        made = documents.create(
            unit, campaign, doc_type=DocumentTypeId.NPC, type_version=1, data=dict(AN_NPC), author=Author.GM
        )
    with db.transaction() as unit:
        documents.seal(unit, campaign, made.id)
    twin = Twin(db, campaigns, sessions, documents, rows, InMemoryAuditLog(), campaign, session, str(made.id))
    return twin, rows


class _Flaky(InMemoryRevealStore):
    """A store whose `write` does its work and then fails, `failures` times:
    the transaction must roll back everything, and the next attempt must start
    afresh."""

    def __init__(self, db: InMemoryDatabase, *, failures: int, error: type[Exception]) -> None:
        super().__init__(db)
        self.failures = failures
        self.error = error
        self.calls = 0

    def write(self, unit: Any, **kwargs: Any) -> Any:
        written = super().write(unit, **kwargs)
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error("injected")
        return written


# ── T-B18: a deadlock victim is retried, then busy ───────────────────────────


def test_a_deadlock_is_retried_three_times_then_busy_and_nothing_is_half_written() -> None:
    """T-B18 (F). A Confirm that loses a deadlock after writing is rolled back
    and tried again in a fresh transaction; the third loss is `RevealBusy`, and
    each failed attempt left nothing behind — no disclosure, no slot, no epoch
    advance, no audit row."""
    twin, flaky = _twin(_Flaky, failures=1, error=psycopg.errors.DeadlockDetected)
    before = twin.epoch()
    answer = twin.confirm(twin.service(flaky))
    assert not answer.replayed and flaky.calls == 2, "one retry, in a fresh transaction"
    assert twin.epoch() == before + 1, "the aborted attempt's epoch advance is gone"
    assert len(twin.live()) == 1 and twin.actions() == ["reveal.displayed"]

    twin, flaky = _twin(_Flaky, failures=3, error=psycopg.errors.DeadlockDetected)
    before = twin.epoch()
    with pytest.raises(RevealBusy):
        twin.confirm(twin.service(flaky))
    assert flaky.calls == 3, "three attempts in all"
    assert (twin.epoch(), twin.live(), twin.shown(), twin.actions()) == (before, [], {}, [])

    twin, flaky = _twin(_Flaky, failures=1, error=psycopg.errors.DeadlockDetected)
    with pytest.raises(RevealBusy):
        twin.confirm(twin.service(flaky, attempts=1))
    assert flaky.calls == 1, "attempts=1 is what the race tests use, so a retry hides nothing there"


def test_a_lock_that_runs_out_is_busy_at_once_and_never_retried() -> None:
    """ID-17: a lock timeout is `RevealBusy` on the first attempt."""
    twin, flaky = _twin(_Flaky, failures=3, error=psycopg.errors.LockNotAvailable)
    with pytest.raises(RevealBusy):
        twin.confirm(twin.service(flaky))
    assert flaky.calls == 1 and twin.live() == [] and twin.actions() == []


def test_a_reveal_service_is_tried_at_least_once() -> None:
    twin, _ = _twin()
    for attempts in (0, -1, True):
        with pytest.raises(ValueError):
            twin.service(attempts=attempts)  # type: ignore[arg-type]


# ── T-B10, critic 13: nothing after the clear can undo a Stop ────────────────


class _PictureFails(InMemoryRevealStore):
    """A store whose picture fails once the Stop's own transaction has
    committed — the `REVEAL_MAX_SLOTS` guard or a driver error."""

    armed = False

    def picture(self, unit: Any, campaign_id: str, *, owner_id: int, now: datetime) -> RevealPicture | None:
        if self.armed:
            raise RevealBusy()
        return super().picture(unit, campaign_id, owner_id=owner_id, now=now)


def test_a_stop_stays_committed_when_its_picture_cannot_be_read() -> None:
    """T-B10's twin case (critic 13). The Stop commits its clear, its epoch and
    its audit row first; a picture that then fails is `RevealBusy`, and what
    the Stop did stays done — a retry is idempotent."""
    twin, rows = _twin(_PictureFails)
    service = twin.service()
    twin.confirm(service)
    before = twin.epoch()
    rows.armed = True
    with pytest.raises(RevealBusy):
        service.stop(twin.campaign, owner_id=1, scope=StopAll(), command_id=_command(), now=datetime.now(UTC))
    assert twin.shown() == {None: None} and twin.live() == []
    assert twin.epoch() == before + 1
    assert twin.actions() == ["reveal.displayed", "reveal.stopped"]


# ── check_mask, pure ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "mask", "at_fault"),
    [
        ({"name": "V", "voice": "low"}, ("name", "voice"), ()),
        ({"name": "V", "tags": ["a"]}, ("tags",), ("tags",)),
        ({"name": "V", "true_identity": "the queen"}, ("true_identity", "name"), ("true_identity",)),
        ({"name": "V"}, ("tell",), ("tell",)),
        ({"name": "V", "attitude": " \t "}, ("attitude",), ("attitude",)),
        ({"name": "V"}, ("all",), ("all",)),
        ({"name": "V"}, ("name", "name"), ("name",)),
        ({"name": "V", "portrait": None}, ("portrait",), ("portrait",)),
        ({"name": "V"}, ("Not A Key",), ("Not A Key",)),
    ],
)
def test_check_mask_names_every_key_at_fault_and_only_those(
    data: dict[str, Any], mask: tuple[str, ...], at_fault: tuple[str, ...]
) -> None:
    assert check_mask(DocumentTypeId.NPC, data, mask) == at_fault


def test_check_mask_uses_the_contracts_per_kind_rule() -> None:
    """Critic 6: a list with one blank item, an entry with a blank text, an
    empty abilities block and a null integer are all absent for a table."""
    stat = {"name": "Ogre", "ac": 11, "hp": 59, "abilities": {}, "xp": None,
            "traits": [{"name": "Brute", "text": "  "}], "senses": "darkvision"}
    assert check_mask(DocumentTypeId.STATBLOCK, stat, ("abilities", "ac", "traits", "xp")) == (
        "abilities",
        "traits",
        "xp",
    )
    lore = {"name": "The Drowned Bell", "rumours": ["it rings at low tide", "   "]}
    assert check_mask(DocumentTypeId.LORE, lore, ("rumours",)) == ("rumours",)
    assert check_mask(DocumentTypeId.LORE, {"rumours": ["it rings"]}, ("rumours",)) == ()


def test_a_display_command_never_shows_its_command_id() -> None:
    command = _command()
    shown = repr(DisplayCommand("cmp_x", "ses_x", command, 0, "doc_x", 1, ("name",), TableAudience()))
    assert command not in shown and "doc_x" in shown


# ── T-B14: every production narrowing names its scope and its clock ─────────


def _tree(name: str) -> ast.Module:
    return ast.parse((SERVICE / name).read_text(encoding="utf-8"))


def _calls_in_functions(tree: ast.Module, attr: str) -> list[tuple[str, ast.Call]]:
    """Every call of `<something>.<attr>(...)` or `<attr>(...)`, with the name
    of the innermost function that makes it."""
    found: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            if isinstance(child, ast.Call):
                func = child.func
                named = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                if named == attr:
                    found.append((owner, child))
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _keywords(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg is not None}


def test_every_production_narrow_names_its_scope_and_every_store_gets_the_fill() -> None:
    """T-B14. Every `.narrow(` in `service/*.py` (tests excluded) passes
    `clears=` and `now=`; every `_advance(` in `table_session_store` passes
    both; no production store is built with `slot_clear=no_slots`; `app.py`
    registers the reconciliation fill and never the empty `reconcile_slots`.

    Positive controls: the scan must find the named sites it exists for —
    `remove_seat`, both archive steps and the reconciliation fill, and
    `narrow`, `_close` and `rotate` in each world — so an empty or mis-rooted
    scan fails. A bead that adds a narrowing (`1kg.5.2`'s document archive and
    delete, `1ir.2.1`'s group removal) must choose its scope here too."""
    modules = sorted(p.name for p in SERVICE.glob("*.py"))
    assert "campaigns_api.py" in modules and "reveals.py" in modules, "the scan is rooted at service/"
    narrows: list[tuple[str, str, set[str]]] = []
    for name in modules:
        for owner, call in _calls_in_functions(_tree(name), "narrow"):
            if isinstance(call.func, ast.Attribute):
                narrows.append((name, owner, _keywords(call)))
    sites = {(name, owner) for name, owner, _ in narrows}
    assert {
        ("campaigns_api.py", "remove_seat"),
        ("campaigns_api.py", "archive_step_one"),
        ("campaigns_api.py", "archive_step_two"),
        ("reveals.py", "reconcile_slots"),
    } <= sites
    missing = [(name, owner) for name, owner, keywords in narrows if not {"clears", "now"} <= keywords]
    assert missing == [], "every production narrow passes clears= and now="

    advances = _calls_in_functions(_tree("table_session_store.py"), "_advance")
    assert sorted(owner for owner, _ in advances) == ["_close", "_close", "narrow", "narrow", "rotate", "rotate"]
    assert all({"clears", "now"} <= _keywords(call) for _, call in advances)

    fills: list[tuple[str, str]] = []
    for name in modules:
        for node in ast.walk(_tree(name)):
            if isinstance(node, ast.keyword) and node.arg == "slot_clear":
                value = node.value
                assert not (isinstance(value, ast.Name) and value.id == "no_slots"), f"{name} passes no_slots"
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    fills.append((name, value.func.id))
    assert {("app.py", "slot_clear_for"), ("campaigns_api.py", "slot_clear_for")} <= set(fills)

    app = (SERVICE / "app.py").read_text(encoding="utf-8")
    assert [owner for owner, _ in _calls_in_functions(_tree("app.py"), "make_reconcile_slots")] == ["_build_stores"]
    assert "reconciliation.reconcile_slots" not in app


# ── T-B16: no reveal module reads a default mask ─────────────────────────────


DEFAULT_MASK_NAMES = ("default_reveal", "defaultReveal", "default_reveal_for")


def test_the_reveal_modules_never_read_a_default_mask() -> None:
    """T-B16 (I-14, X-2, REVEAL-4). Registry defaults seed the GM's draft on
    the client and nothing on the server. Positive control: the scan does find
    the names where they live, so a scan that reads nothing cannot pass."""
    for name in REVEAL_MODULES:
        source = (SERVICE / name).read_text(encoding="utf-8")
        assert len(source) > 1000, f"{name} was read"
        assert [n for n in DEFAULT_MASK_NAMES if n in source] == [], name
    registry = (SERVICE / "workbench_registry.py").read_text(encoding="utf-8")
    assert "default_reveal_for" in registry


def test_the_audit_actions_the_service_writes_are_the_three_reveal_actions() -> None:
    """The service writes only `reveal.displayed`, `reveal.updated` and
    `reveal.stopped` — read from its source, so a fourth cannot creep in."""
    written = {
        node.attr
        for node in ast.walk(_tree("reveals.py"))
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "AuditAction"
    }
    assert written == {"REVEAL_DISPLAYED", "REVEAL_UPDATED", "REVEAL_STOPPED"}
    assert {AuditAction[name].value for name in written} == {"reveal.displayed", "reveal.updated", "reveal.stopped"}
