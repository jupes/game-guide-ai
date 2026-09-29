"""The eligibility records, the retry policy and three tripwires (1ir.2.1),
without a database.

The behaviour both worlds share, and PostgreSQL's races, are
`tests/test_eligibility_db.py`'s. What is here runs anywhere: what an
`Eligibility` can be built as, how the service answers a lost deadlock and a
lock timeout (against a stub database), and three source scans — the AC's
negative clause (consent, attestation, announcement and pause writes never
advance the revision), "only the eligibility service writes eligibility and
group state", and "only the helper writes `projection_revision`". Each scan is
proven on a planted source first, so a checker that always answers "nothing"
fails its own test.
"""

from __future__ import annotations

import ast
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from service import eligibility_store as es
from service.campaign_store import InMemoryCampaignStore
from service.db import InMemoryDatabase, ProjectionItem
from service.document_store import InMemoryDocumentStore
from service.eligibility import (
    DEADLOCK_ATTEMPTS,
    BackendUnavailable,
    Change,
    EligibilityMutations,
    NotAppliedYet,
    no_record,
    no_table_namespace,
)
from service.eligibility_store import (
    GM_ONLY_BY_CONSTRUCTION,
    UNCLASSIFIED,
    ClassificationSource,
    Eligibility,
    FieldClass,
    InMemoryEligibilityStore,
    classifiable_keys,
    principals,
)
from service.participant_store import InMemoryParticipantStore
from service.table_session_store import InMemoryTableSessionStore, no_slots
from service.workbench_contracts import Author, DocumentTypeId

SERVICE = Path(__file__).resolve().parents[1]
GM = ClassificationSource.GM
P = sorted("prt_" + c * 22 for c in "abc")


# ── What an Eligibility can be (T-D9, critic item 2) ─────────────────────────


def test_the_module_constants_build_and_the_sentinel_is_the_only_sourceless_gm_only():
    assert UNCLASSIFIED == Eligibility(FieldClass.UNCLASSIFIED)
    assert GM_ONLY_BY_CONSTRUCTION.source is None
    assert not UNCLASSIFIED.admits_anyone and not GM_ONLY_BY_CONSTRUCTION.admits_anyone
    assert Eligibility(FieldClass.PUBLIC, (), GM).admits_anyone


@pytest.mark.parametrize(
    ("build", "refusal"),
    [
        pytest.param(lambda: Eligibility(FieldClass.PARTICIPANTS, (P[1], P[0]), GM), ValueError, id="unsorted"),
        pytest.param(lambda: Eligibility(FieldClass.PARTICIPANTS, (P[0], P[0]), GM), ValueError, id="repeated"),
        pytest.param(lambda: Eligibility(FieldClass.PARTICIPANTS, (), GM), ValueError, id="empty-list"),
        pytest.param(
            lambda: Eligibility(FieldClass.PARTICIPANTS, tuple(f"prt_{n:022d}" for n in range(101)), GM),
            ValueError,
            id="101-ids",
        ),
        pytest.param(lambda: Eligibility(FieldClass.GROUPS, (P[0],), GM), ValueError, id="wrong-kind"),
        pytest.param(lambda: Eligibility(FieldClass.PUBLIC, (P[0],), GM), ValueError, id="list-on-public"),
        pytest.param(lambda: Eligibility(FieldClass.PUBLIC), ValueError, id="no-source"),
        pytest.param(lambda: Eligibility(FieldClass.UNCLASSIFIED, (), GM), ValueError, id="sourced-unclassified"),
        pytest.param(
            lambda: Eligibility(FieldClass.CAMPAIGN, (), ClassificationSource.DEFAULT), ValueError, id="default-widens"
        ),
        pytest.param(lambda: Eligibility("public", (), GM), TypeError, id="class-not-enum"),  # type: ignore[arg-type]
        pytest.param(lambda: Eligibility(FieldClass.PARTICIPANTS, [P[0]], GM), TypeError, id="ids-not-tuple"),  # type: ignore[arg-type]
        pytest.param(lambda: Eligibility(FieldClass.PUBLIC, (), "gm"), TypeError, id="source-not-enum"),  # type: ignore[arg-type]
    ],
)
def test_an_eligibility_the_database_would_refuse_cannot_be_built(build, refusal):
    """T-D9: kills a sort dropped from `__post_init__` (M-D13); no refusal
    names an id."""
    with pytest.raises(refusal) as refused:
        build()
    assert "prt_" not in str(refused.value)


def test_principals_puts_ids_into_the_canonical_order():
    assert principals(FieldClass.PARTICIPANTS, list(reversed(P))).ids == tuple(P)
    assert Eligibility(FieldClass.GM_ONLY, (), ClassificationSource.DEFAULT).source is ClassificationSource.DEFAULT


def test_an_unknown_type_has_no_classifiable_key():
    """I-20: evaluation fails closed on a type this build does not know."""
    db = InMemoryDatabase()
    documents = InMemoryDocumentStore(db)
    with db.transaction() as unit:
        campaign = InMemoryCampaignStore(db).create(unit, owner_id=1, name="N").id
        record = documents.create(
            unit, campaign, doc_type=DocumentTypeId.LORE, type_version=1, data={"name": "Bell"}, author=Author.GM
        )
    assert classifiable_keys(record)
    assert classifiable_keys(replace(record, type="nonesuch")) == frozenset()
    assert classifiable_keys(replace(record, type_version=2)) == frozenset()


def test_a_wrong_type_is_refused_by_parameter_name_and_never_by_value():
    for given in ({"campaign_id": 5}, {"document": "doc"}, {"eligibility": "public"}, {"limit": True}):
        with pytest.raises(TypeError) as refused:
            es.check_types(**given)
        assert next(iter(given)) in str(refused.value)
    es.check_types(command_id=None, after_id=0, limit=1, campaign_id="cmp_x")


# ── The retry policy (T-F9, critic item 5) ───────────────────────────────────


class Flaky:
    """A `TransactionalDatabase` over the twin whose Nth transaction raises the
    Nth planned error on entry; `None` lets it through."""

    def __init__(self, db: InMemoryDatabase, plan: list[type[Exception] | None]) -> None:
        self.db = db
        self.plan = plan
        self.opened = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        planned = self.plan[self.opened] if self.opened < len(self.plan) else None
        self.opened += 1
        if planned is not None:
            raise planned("the driver's own text, which quotes a row")
        with self.db.transaction() as unit:
            yield unit


def _service(plan: list[type[Exception] | None]) -> tuple[EligibilityMutations, Flaky, dict[str, str]]:
    db = InMemoryDatabase()
    documents = InMemoryDocumentStore(db)
    eligibility = InMemoryEligibilityStore(db, documents=documents)
    with db.transaction() as unit:
        campaign = InMemoryCampaignStore(db).create(unit, owner_id=1, name="N").id
        seat = InMemoryParticipantStore(db).add(unit, campaign, alias="Rook").id
        sheet = documents.create(
            unit, campaign, doc_type=DocumentTypeId.CHARACTER_SHEET, type_version=1, data={"name": "Rook"},
            author=Author.GM,
        ).id
    flaky = Flaky(db, plan)
    service = EligibilityMutations(
        flaky,
        eligibility=eligibility,
        documents=documents,
        sessions=InMemoryTableSessionStore(db, slot_clear=no_slots),
        table_namespace=no_table_namespace,
        record=no_record,
    )
    return service, flaky, {"campaign": campaign, "seat": seat, "sheet": sheet}


DEADLOCK = psycopg.errors.DeadlockDetected
LOCK_TIMEOUT = psycopg.errors.LockNotAvailable
PUBLIC = Eligibility(FieldClass.PUBLIC, (), GM)


def test_a_deadlock_victim_is_retried_and_the_result_returned():
    """Two lost deadlocks, then the third attempt's result (I-21: the number
    is `table_sessions`' three, written out so the test cannot follow a change
    to the constant). Kills no retry (M-F12)."""
    assert DEADLOCK_ATTEMPTS == 3
    service, flaky, ids = _service([DEADLOCK, DEADLOCK])
    change = service.classify(ids["campaign"], ids["sheet"], "hp", expected=UNCLASSIFIED, new=PUBLIC)
    assert change == Change(True, 1)
    assert flaky.opened == 3


def test_a_deadlock_lost_every_time_is_backend_unavailable_and_bounded(caplog):
    """Kills no retry (M-F12) and an unbounded retry (M-F13); only the
    SQLSTATE is logged, and nothing is chained."""
    service, flaky, ids = _service([DEADLOCK] * (DEADLOCK_ATTEMPTS + 2))
    with caplog.at_level(logging.WARNING, logger="service.eligibility"):
        with pytest.raises(BackendUnavailable) as refused:
            service.classify(ids["campaign"], ids["sheet"], "hp", expected=UNCLASSIFIED, new=PUBLIC)
    assert flaky.opened == DEADLOCK_ATTEMPTS
    assert refused.value.__context__ is None and refused.value.__cause__ is None
    assert all("40P01" in r.getMessage() and "quotes a row" not in r.getMessage() for r in caplog.records)
    assert len(caplog.records) == DEADLOCK_ATTEMPTS


def test_a_lock_timeout_on_a_widening_is_answered_at_once():
    service, flaky, ids = _service([LOCK_TIMEOUT])
    with pytest.raises(BackendUnavailable):
        service.classify(ids["campaign"], ids["sheet"], "hp", expected=UNCLASSIFIED, new=PUBLIC)
    assert flaky.opened == 1


def test_step_two_is_attempted_exactly_once_after_a_lock_timeout():
    """Critic item 5: step 1 commits, step 2 meets a lock timeout once and is
    answered `NotAppliedYet` — never retried inside the request."""
    service, flaky, ids = _service([None, LOCK_TIMEOUT])
    group = "grp_" + "g" * 22
    with pytest.raises(NotAppliedYet) as refused:
        service.remove_member(ids["campaign"], group, ids["seat"])
    assert flaky.opened == 2
    assert refused.value.__context__ is None


def test_step_two_losing_every_deadlock_is_not_applied_yet():
    service, flaky, ids = _service([None, *[DEADLOCK] * DEADLOCK_ATTEMPTS])
    with pytest.raises(NotAppliedYet):
        service.remove_group(ids["campaign"], "grp_" + "g" * 22)
    assert flaky.opened == 1 + DEADLOCK_ATTEMPTS


def test_step_two_never_runs_when_step_one_fails():
    service, flaky, ids = _service([DEADLOCK] * DEADLOCK_ATTEMPTS + [None])
    with pytest.raises(BackendUnavailable):
        service.remove_member(ids["campaign"], "grp_" + "g" * 22, ids["seat"])
    assert flaky.opened == DEADLOCK_ATTEMPTS


def test_a_malformed_key_is_refused_before_any_transaction():
    service, flaky, ids = _service([])
    for key in ("all", "a.b", "Name"):
        with pytest.raises(es.NotClassifiable):
            service.classify(ids["campaign"], ids["sheet"], key, expected=UNCLASSIFIED, new=PUBLIC)
    with pytest.raises(TypeError):
        service.classify(ids["campaign"], ids["sheet"], "hp", expected="unclassified", new=PUBLIC)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="group name"):
        service.rename_group(ids["campaign"], "grp_" + "g" * 22, name="")
    with pytest.raises(ValueError, match="command id"):
        service.create_group(ids["campaign"], name="Scouts", command_id="short")
    assert flaky.opened == 0


# ── Tripwires (T-G1 to T-G3, critic item 11) ─────────────────────────────────


def _modules() -> Iterator[tuple[Path, str]]:
    for path in sorted(SERVICE.rglob("*.py")):
        if "tests" in path.relative_to(SERVICE).parts:
            continue
        yield path, path.read_text(encoding="utf-8")


_CAPTURE_GATES = re.compile(r"consent|attest|announce|pause", re.IGNORECASE)


def capture_gate_violations(source: str, module: str = "") -> list[str]:
    """Every function, method or class named for consent, attestation,
    announcement or pause — or every line of a module so named — that refers
    to `advance_authz_revision` or calls `lock_campaign` with `shared` not the
    literal True. Plan section 4.3 rule 1: those writes gate capture, not
    visibility, so they never advance the revision nor take the lock to."""
    tree = ast.parse(source)
    scopes: list[ast.AST] = [tree] if _CAPTURE_GATES.search(module) else []
    scopes += [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and _CAPTURE_GATES.search(node.name)
    ]
    found: list[str] = []
    for scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Attribute | ast.Name) and "advance_authz_revision" in (
                getattr(node, "attr", None),
                getattr(node, "id", None),
            ):
                found.append(f"{module}:{node.lineno} advances the revision")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "lock_campaign"
                and not any(
                    k.arg == "shared" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords
                )
            ):
                found.append(f"{module}:{node.lineno} takes the campaign lock exclusively")
    return found


PLANTED_CAPTURE = '''
def record_consent(unit, campaign_id):
    unit.lock_campaign(campaign_id, shared=True)
    unit.advance_authz_revision(campaign_id)

class Attestations:
    def write(self, unit, campaign_id):
        unit.lock_campaign(campaign_id, shared=False)

def pause_capture(unit, campaign_id):
    unit.lock_campaign(campaign_id, shared=True)

def reveal(unit, campaign_id):
    unit.lock_campaign(campaign_id, shared=False)
    unit.advance_authz_revision(campaign_id)
'''


def test_consent_attestation_announcement_and_pause_writes_never_advance_the_revision():
    """T-G1, the AC's negative clause. The checker is proven on a planted
    source first (kills a checker that always answers nothing, M-G1): a
    consent write that advances, an attestation that locks exclusively, and
    nothing for a share-mode pause read or for an ordinary reveal."""
    planted = capture_gate_violations(PLANTED_CAPTURE, "planted")
    assert planted == ["planted:4 advances the revision", "planted:8 takes the campaign lock exclusively"]
    found = [v for path, source in _modules() for v in capture_gate_violations(source, path.stem)]
    assert found == []


_WRITES = {"put", "create_group", "rename_group", "remove_group", "add_member", "remove_member"}
_STORES = {"PostgresEligibilityStore", "InMemoryEligibilityStore"}


def eligibility_write_violations(source: str, module: str) -> list[str]:
    """Calls of an eligibility store's writes — an attribute call whose
    receiver's source names `eligibility` — and imports of a concrete store,
    outside the two modules that own them (and `app.py`, for the import)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _WRITES
            and "eligibility" in ast.unparse(node.func.value)
        ):
            found.append(f"{module}:{node.lineno} writes eligibility state")
        if module != "app" and isinstance(node, ast.ImportFrom) and any(a.name in _STORES for a in node.names):
            found.append(f"{module}:{node.lineno} imports a concrete eligibility store")
    return found


PLANTED_WRITE = '''
from service.eligibility_store import PostgresEligibilityStore

@router.put("/x")
def route(stores, unit, record, key, value):
    stores.eligibility.put(unit, record, key, value)
    stores.documents.put(unit)
'''


def test_only_the_eligibility_service_writes_eligibility_and_group_state():
    """T-G2: proven on a planted violation (kills M-G2 and a checker that flags
    `router.put`), then run over `service/`."""
    assert eligibility_write_violations(PLANTED_WRITE, "planted") == [
        "planted:2 imports a concrete eligibility store",
        "planted:6 writes eligibility state",
    ]
    found = [
        v
        for path, source in _modules()
        if path.name not in ("eligibility.py", "eligibility_store.py")
        for v in eligibility_write_violations(source, path.stem)
    ]
    assert found == []


def _sql(source: str) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(
                part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )


_SET_CLAUSE = re.compile(r"\bSET\b(.*?)(?:\bWHERE\b|\bRETURNING\b|\bFROM\b|$)", re.IGNORECASE | re.DOTALL)
_INSERT_COLUMNS = re.compile(r"\bINSERT\s+INTO\s+\S+\s*\(([^)]*)\)", re.IGNORECASE)


def projection_writes(source: str, module: str) -> list[str]:
    """SQL that writes `projection_revision`: in a SET clause as an assignment
    target, or in an INSERT's column list. A read predicate is not a write."""
    found: list[str] = []
    for line, text in _sql(source):
        sets = [clause for clause in _SET_CLAUSE.findall(text) if re.search(r"\bprojection_revision\s*=", clause)]
        inserts = [cols for cols in _INSERT_COLUMNS.findall(text) if "projection_revision" in cols]
        if sets or inserts:
            found.append(f"{module}:{line}")
    return found


PLANTED_PROJECTION = '''
A = "UPDATE campaign.authz_state SET projection_revision = authz_revision WHERE campaign_id = %s"
B = "INSERT INTO campaign.authz_state (campaign_id, projection_revision) VALUES (%s, 0)"
C = "SELECT 1 FROM campaign.authz_state WHERE projection_revision = authz_revision"
D = "UPDATE campaign.authz_state SET authz_revision = 1 WHERE projection_revision = 0"
'''


def test_projection_revision_is_written_only_by_the_helper():
    """T-G3: proven in both directions on planted SQL (kills M-G3), then run
    over `service/`: every write is in `db.py`. `1ir.2.3` amends this test when
    the projector joins it."""
    assert projection_writes(PLANTED_PROJECTION, "planted") == ["planted:2", "planted:3"]
    writers = {v.split(":")[0] for path, source in _modules() for v in projection_writes(source, path.stem)}
    assert writers == {"db"}


def test_the_eligibility_modules_write_no_audit_row_and_enqueue_no_job():
    """I-17 and SEC-38: routes write audit rows; nothing here imports the
    audit writer or the job queue."""
    for name in ("eligibility.py", "eligibility_store.py"):
        source = (SERVICE / name).read_text(encoding="utf-8")
        assert "audit_log" not in source and "from .jobs" not in source, name


def test_nothing_is_stamped_with_a_clock_the_caller_did_not_give():
    """A small guard on the twin's queue stamp: it is aware UTC."""
    db = InMemoryDatabase()
    db.authz_state["cmp_one"] = 0
    with db.transaction() as unit:
        unit.lock_campaign("cmp_one", shared=False)
        unit.advance_authz_revision("cmp_one", project=[ProjectionItem("doc_" + "d" * 22, "hp")])
        stamp = unit.projected_items("cmp_one")[0].created_at
    assert stamp.tzinfo is not None and stamp <= datetime.now(UTC)
