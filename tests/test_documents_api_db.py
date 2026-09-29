"""The document routes' compositions against a real PostgreSQL (bead 1kg.5.2).

What no twin can prove: who waits for whom. Every race here is explicit — a
third connection holds a row, the requests under test are started, the SERVER
is asked (`pg_stat_activity`) whether they are really blocked, and only then is
the row let go — so nothing depends on timing. And one thing the twin can only
claim: that its library and history orders are PostgreSQL's, ties included.

The routes' answers over the twins are `service/tests/test_documents_api.py`'s;
the store's own behaviour in both worlds is `tests/test_document_db.py`'s.

Requires DATABASE_URL (CI sets it for this file, `.github/workflows/ci.yml`,
pinned by `service/tests/test_ci_workflow.py`). Without it every test skips.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _pg import connect, needs_db, throwaway_database
from fastapi import HTTPException

from service import campaign_identity as ident
from service import migrations as mig
from service.campaign_store import InMemoryCampaignStore, PostgresCampaignStore
from service.db import CampaignLockSettings, Database, InMemoryDatabase, PoolSettings
from service.document_store import SEAL_IDLE_S, InMemoryDocumentStore, PostgresDocumentStore
from service.document_wire import decode_history_cursor, decode_library_cursor
from service.documents_api import (
    DocumentStores,
    create_document,
    patch_document,
    query_library,
    read_history,
    restore_document,
    seal_document,
)
from service.workbench_contracts import (
    Document,
    DocumentCreateRequest,
    FieldPatchRequest,
    LibraryQuery,
)

pytestmark = needs_db

CAMPAIGN = "cmp_" + "d" * 22
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
#: The interleaving tests wait for a THREAD, not for the database, so RQ-8's
#: five seconds would race the harness.
PATIENT = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=30)
#: RQ-8's own bounds: a content write that asked for the campaign lock would be
#: refused within a second.
QUICK = CampaignLockSettings(lock_timeout_s=1, transaction_timeout_s=5)
PATIENCE = 15
_COMMANDS = itertools.count()


@pytest.fixture
def dsn() -> Iterator[str]:
    with throwaway_database("docsapi") as target:
        mig.migrate(target)
        yield target


@pytest.fixture
def owner(dsn: str) -> int:
    with connect(dsn) as conn:
        gm = int(conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x') RETURNING id"
        ).fetchone()[0])
        conn.execute("INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
                     (CAMPAIGN, gm))
    return gm


def _database(dsn: str, settings: CampaignLockSettings = PATIENT) -> Database:
    return Database(dsn, PoolSettings(sync_max=6, async_max=0, acquire_timeout_s=5), settings)


def _stores() -> DocumentStores:
    return DocumentStores(PostgresCampaignStore(), PostgresDocumentStore())


def _command() -> str:
    return f"cmd-db-{next(_COMMANDS):012d}-abc"


def _create(db: Any, stores: DocumentStores, owner: int, data: dict[str, Any] | None = None, *,
            command_id: str | None = None, doc_type: str = "npc", now: datetime = NOW) -> Document:
    request = DocumentCreateRequest.model_validate({
        "schema_version": 1, "command_id": command_id or _command(), "campaign_id": CAMPAIGN, "type": doc_type,
        "type_version": 1, "data": data if data is not None else {"name": "Mira"}})
    return create_document(db, stores, campaign_id=CAMPAIGN, owner_id=owner, request=request, now=now)


def _patch(db: Any, stores: DocumentStores, owner: int, document: str, base: int, fields: dict[str, Any],
           now: datetime = NOW) -> Document:
    request = FieldPatchRequest.model_validate({
        "schema_version": 1, "type": "npc", "type_version": 1, "base_write_revision": base, "fields": fields})
    return patch_document(db, stores, campaign_id=CAMPAIGN, document_id=document, owner_id=owner,
                          request=request, now=now)


def _waiting(dsn: str, waiters: int) -> bool:
    """Whether at least `waiters` backends on this database are blocked on a
    lock — the server's own account of it."""
    deadline = time.monotonic() + PATIENCE
    with connect(dsn) as conn:
        while time.monotonic() < deadline:
            blocked = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).fetchone()[0]
            if blocked >= waiters:
                return True
            time.sleep(0.05)
    return False


@contextmanager
def _holding(dsn: str, statement: str, params: tuple[Any, ...]) -> Iterator[None]:
    """A third connection that runs `statement` and keeps its transaction open
    until the block ends, then commits."""
    took, release = threading.Event(), threading.Event()
    failure: list[BaseException] = []

    def hold() -> None:
        try:
            with connect(dsn, autocommit=False) as conn:
                conn.execute(statement, params)
                took.set()
                release.wait(PATIENCE)
                conn.commit()
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            failure.append(exc)
            took.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert took.wait(PATIENCE), "the holder never took its lock"
    if failure:
        raise failure[0]
    try:
        yield
    finally:
        release.set()
        thread.join(PATIENCE)
    if failure:
        raise failure[0]


def _holding_the_document(dsn: str, document: str) -> Any:
    return _holding(dsn, "SELECT 1 FROM campaign.documents WHERE id = %s FOR NO KEY UPDATE", (document,))


def _racing(dsn: str, hold: Callable[[], Any], *works: Callable[[], object]) -> list[object]:
    """Start every `work` while `hold` is held, see them all waiting, release,
    and return what each produced — its answer, or the exception it raised."""
    produced: list[list[object]] = [[] for _ in works]
    threads: list[threading.Thread] = []

    def run(index: int, work: Callable[[], object]) -> None:
        try:
            produced[index].append(work())
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            produced[index].append(exc)

    with hold():
        for index, work in enumerate(works):
            thread = threading.Thread(target=run, args=(index, work), daemon=True)
            thread.start()
            threads.append(thread)
        assert _waiting(dsn, len(works)), "nobody was blocked, so this proves nothing"
    for thread in threads:
        thread.join(PATIENCE)
        assert not thread.is_alive(), "a transaction never finished"
    return [outcome[0] for outcome in produced]


def _count(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> int:
    with connect(dsn) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


# ── P-1, P-2, P-11: two autosaves of one document ───────────────────────────


def test_two_patches_of_different_keys_on_one_base_both_land(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    made = _create(db, stores, owner)
    base = made.write_revision
    outcomes = _racing(
        dsn, lambda: _holding_the_document(dsn, made.document_id),
        lambda: _patch(db, stores, owner, made.document_id, base, {"voice": "low"}),
        lambda: _patch(db, stores, owner, made.document_id, base, {"tell": "hums"}),
    )
    assert all(isinstance(outcome, Document) for outcome in outcomes), outcomes
    final = max((o for o in outcomes if isinstance(o, Document)), key=lambda d: d.write_revision)
    assert final.write_revision == base + 2
    assert {k: final.data[k] for k in ("voice", "tell")} == {"voice": "low", "tell": "hums"}


def test_two_patches_of_one_key_on_one_base_have_one_winner_and_one_conflict(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    made = _create(db, stores, owner)
    base = made.write_revision
    outcomes = _racing(
        dsn, lambda: _holding_the_document(dsn, made.document_id),
        lambda: _patch(db, stores, owner, made.document_id, base, {"voice": "low"}),
        lambda: _patch(db, stores, owner, made.document_id, base, {"voice": "high"}),
    )
    won = [o for o in outcomes if isinstance(o, Document)]
    lost = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(won) == 1 and len(lost) == 1, outcomes
    assert lost[0].status_code == 409
    assert lost[0].detail["conflict"] == {"write_revision": base + 1, "fields": ["voice"]}
    assert won[0].write_revision == base + 1
    with connect(dsn) as conn:
        stored = conn.execute("SELECT data->>'voice', write_revision FROM campaign.documents WHERE id = %s",
                              (made.document_id,)).fetchone()
    assert stored == (won[0].data["voice"], base + 1), "the loser wrote nothing"


def test_a_retried_autosave_in_flight_beside_its_original_is_a_no_op(dsn: str, owner: int) -> None:
    """CANVAS-10's retried autosave: the second of two identical patches reads
    under the row lock what the first committed, finds nothing to change, and
    answers the document — never a conflict with itself."""
    db, stores = _database(dsn), _stores()
    made = _create(db, stores, owner)
    base = made.write_revision
    outcomes = _racing(
        dsn, lambda: _holding_the_document(dsn, made.document_id),
        lambda: _patch(db, stores, owner, made.document_id, base, {"voice": "low"}),
        lambda: _patch(db, stores, owner, made.document_id, base, {"voice": "low"}),
    )
    assert all(isinstance(outcome, Document) for outcome in outcomes), outcomes
    assert {o.write_revision for o in outcomes if isinstance(o, Document)} == {base + 1}
    assert _count(dsn, "SELECT count(*) FROM campaign.document_versions WHERE document_id = %s",
                  (made.document_id,)) == 1


# ── P-3: content writes never wait for the campaign lock ────────────────────


def test_content_writes_commit_while_another_transaction_holds_the_campaign_lock(dsn: str, owner: int) -> None:
    db, stores = _database(dsn, QUICK), _stores()
    made = _create(db, stores, owner)
    later = NOW + timedelta(seconds=SEAL_IDLE_S)
    moved = _patch(db, stores, owner, made.document_id, made.write_revision, {"voice": "low"}, later)
    assert moved.version.number == 2
    with _holding(dsn, "SELECT 1 FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE", (CAMPAIGN,)):
        created = _create(db, stores, owner, {"name": "Rook"})
        patched = _patch(db, stores, owner, made.document_id, moved.write_revision, {"tell": "hums"}, later)
        restored = restore_document(db, stores, campaign_id=CAMPAIGN, document_id=made.document_id,
                                    owner_id=owner, version_number=1, now=later)
        sealed = seal_document(db, stores, campaign_id=CAMPAIGN, document_id=created.document_id, owner_id=owner,
                               now=later)
    assert patched.write_revision == moved.write_revision + 1
    assert restored.version.restored_from == 1 and sealed.version.sealed is True
    assert _count(dsn, "SELECT count(*) FROM campaign.documents") == 2


# ── P-4: one command id, two creates in flight ──────────────────────────────


def test_two_creates_with_one_command_id_make_one_document(dsn: str, owner: int) -> None:
    db, stores = _database(dsn), _stores()
    key = _command()
    outcomes = _racing(
        dsn, lambda: _holding(dsn, "SELECT 1 FROM campaign.campaigns WHERE id = %s FOR UPDATE", (CAMPAIGN,)),
        lambda: _create(db, stores, owner, {"name": "First"}, command_id=key),
        lambda: _create(db, stores, owner, {"name": "Second"}, command_id=key),
    )
    assert all(isinstance(outcome, Document) for outcome in outcomes), outcomes
    assert len({o.document_id for o in outcomes if isinstance(o, Document)}) == 1
    assert _count(dsn, "SELECT count(*) FROM campaign.documents WHERE created_command_id = %s", (key,)) == 1


# ── P-5: the twin's orders are PostgreSQL's ─────────────────────────────────

#: Ids whose code-point order differs from a case-insensitive one, so a
#: collation other than "C" would show.
_IDS = ["doc_" + body * 22 for body in ("B", "a", "A", "b", "Z", "c", "0", "_")]
#: Names that fold together: by case, and by NFKC (a ligature).
_NAMES = ["fire", "Fire", chr(0xFB01) + "re", "FIRE", "ember", "Ember", "ash", "Ash"]


def _pages(db: Any, stores: DocumentStores, owner: int, document: str) -> list[list[str]]:
    pages: list[list[str]] = []
    for sort in ("recent", "name"):
        cursor: str | None = None
        while True:
            query = LibraryQuery.model_validate({
                "schema_version": 1, "campaign_id": CAMPAIGN, "category": "npcs", "search": "", "sort": sort,
                "archived": False, "limit": 3, **({"cursor": cursor} if cursor else {})})
            page = query_library(db, stores, campaign_id=CAMPAIGN, owner_id=owner, query=query,
                                 after_id=None if cursor is None else decode_library_cursor(cursor))
            pages.append([item.document_id for item in page.items])
            cursor = page.next_cursor
            if cursor is None:
                break
    before: int | None = None
    while True:
        history = read_history(db, stores, campaign_id=CAMPAIGN, document_id=document, owner_id=owner,
                               before_number=before, limit=2)
        pages.append([str(item.number) for item in history.items])
        if history.next_cursor is None:
            return pages
        before = decode_history_cursor(history.next_cursor, document)


def _seeded(db: Any, stores: DocumentStores, owner: int) -> str:
    """Eight documents, the first four written at one moment (every Recent row
    of them ties), the rest a minute apart; the first grows five versions."""
    for index, name in enumerate(_NAMES):
        _create(db, stores, owner, {"name": name}, now=NOW + timedelta(minutes=max(0, index - 3)))
    first = _IDS[0]
    base = 1
    for step in range(1, 5):
        base = _patch(db, stores, owner, first, base, {"voice": f"take {step}"},
                      NOW + timedelta(hours=1, seconds=SEAL_IDLE_S * step)).write_revision
    return first


def test_the_twin_pages_the_library_and_history_exactly_as_postgresql_does(
    dsn: str, owner: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def minted() -> Callable[[str], str]:
        ids = iter(_IDS)
        return lambda prefix: CAMPAIGN if prefix == ident.CAMPAIGN else next(ids)

    monkeypatch.setattr(ident, "new_id", minted())
    postgres = _pages(_database(dsn), _stores(), owner, _seeded(_database(dsn), _stores(), owner))

    monkeypatch.setattr(ident, "new_id", minted())
    twin_db = InMemoryDatabase()
    twin = DocumentStores(InMemoryCampaignStore(twin_db), InMemoryDocumentStore(twin_db))
    with twin_db.transaction() as unit:
        assert twin.campaigns.create(unit, owner_id=owner, name="Nocturne", now=NOW).id == CAMPAIGN
    in_memory = _pages(twin_db, twin, owner, _seeded(twin_db, twin, owner))

    assert postgres == in_memory
    assert sorted(i for page in postgres[:3] for i in page) == sorted(_IDS), "the Recent walk saw every row once"


# ── Bead ssr (PR #173 review M-1): restoring a version this build refuses ────


def test_restoring_a_damaged_version_is_document_unsupported_and_writes_nothing(dsn: str, owner: int) -> None:
    """The chosen version fails the whole-document validation in the store:
    `409 document_unsupported`, and the transaction that held the row appends
    no version and changes neither the data nor the write revision."""
    db, stores = _database(dsn), _stores()
    made = _create(db, stores, owner)
    later = NOW + timedelta(seconds=SEAL_IDLE_S)
    moved = _patch(db, stores, owner, made.document_id, made.write_revision, {"voice": "low"}, later)
    assert moved.version.number == 2
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE campaign.document_versions SET data = %s::jsonb WHERE document_id = %s AND number = 1",
            (json.dumps({"name": "Mira", "secret_ally": "x"}), made.document_id),
        )
    with pytest.raises(HTTPException) as refused:
        restore_document(db, stores, campaign_id=CAMPAIGN, document_id=made.document_id, owner_id=owner,
                         version_number=1, now=later + timedelta(seconds=1))
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "document_unsupported"  # type: ignore[index]
    assert _count(dsn, "SELECT count(*) FROM campaign.document_versions WHERE document_id = %s",
                  (made.document_id,)) == 2
    with connect(dsn) as conn:
        stored = conn.execute("SELECT data, write_revision, updated_at FROM campaign.documents WHERE id = %s",
                              (made.document_id,)).fetchone()
    assert stored == ({"name": "Mira", "voice": "low"}, moved.write_revision, later)
