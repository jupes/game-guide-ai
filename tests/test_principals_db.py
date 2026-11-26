"""Re-read standing through the real stores and shadow the table read gate."""

from __future__ import annotations

import random
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from _pg import connect, needs_db
from _policy_worlds import WrittenWorld, write
from test_eligibility_db import World, _campaign, _document, _group, _live_session, _seat
from test_eligibility_db import dsn as dsn
from test_eligibility_db import world as world
from test_policy_facts_db import _assert_unavailable, _CountingConnection

from service import campaign_identity as ident
from service import policy
from service.campaign_store import MissingParent
from service.db import Database, PgTransaction, PoolSettings
from service.policy_facts import PolicyUnavailable
from service.principals import InMemoryPrincipalFacts, PostgresPrincipalFacts, PrincipalResolver
from service.reveal_store import PostgresRevealStore
from service.session import SessionData
from service.table_session_store import PostgresTableSessionStore, no_slots
from service.tests import pdp_fixtures as pdp
from service.tests.policy_oracle import (
    ENT_SESSIONS,
    ENTITLEMENT_MATRIX,
    Release,
    Requester,
    Slot,
    State,
    entitled,
    entitlement_requester,
    entitlement_slot,
    entitlement_state,
    gen_entitlement_case,
    gen_world,
    table_principal,
)


def _facts(world: World) -> InMemoryPrincipalFacts | PostgresPrincipalFacts:
    if world.kind == "fake":
        return InMemoryPrincipalFacts(world.db, documents=world.documents)
    return PostgresPrincipalFacts()


def _account(world: World) -> SessionData:
    if world.dsn is None:
        return SessionData(world.owner + 1, "player")
    with connect(world.dsn) as conn:
        row = conn.execute(
            "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id",
            (f"{secrets.token_hex(8)}@example.test",),
        ).fetchone()
    assert row is not None
    return SessionData(row[0], "player")


def test_owner_role_campaign_and_revisions_are_read_on_each_call(world: World) -> None:
    campaign = _campaign(world)
    other_campaign = _campaign(world)
    session = _live_session(world, campaign)
    owner = SessionData(world.owner, "dm")
    stranger = _account(world)
    facts = _facts(world)
    resolver = PrincipalResolver(world.db, facts)
    for account in (owner, SessionData(world.owner, "player")):
        standing = resolver.table(campaign, account=account, screen=None)
        assert isinstance(standing, policy.TablePrincipal)
        assert (standing.kind, standing.scope, standing.session_id, standing.generation) == (
            policy.TableKind.OWNER_VIEWER, None, session, 1
        )
        assert standing.characters == standing.groups == frozenset()
        assert standing.authz_revision == standing.projection_revision == 0

    with world.db.transaction() as unit:
        assert facts.gm_facts(unit, campaign, user_id=stranger.user_id) is None
        unit.lock_campaign(campaign, shared=False)
        unit.advance_authz_revision(campaign)
        assert world.campaigns.set_archived(unit, campaign, owner_id=owner.user_id, archived=True)
    gm = resolver.gm(campaign, account=owner)
    assert (gm.campaign_id, gm.archived, gm.authz_revision, gm.projection_revision) == (campaign, True, 1, 1)
    assert not resolver.gm(other_campaign, account=owner).archived
    assert resolver.gm(other_campaign, account=owner).authz_revision == 0
    for key, account in ((campaign, stranger), (ident.new_id(ident.CAMPAIGN), owner), ("invalid", owner)):
        with pytest.raises(MissingParent):
            resolver.gm(key, account=account)


def test_seat_confirmation_removed_memberships_sheets_and_seat_removal(world: World) -> None:
    campaign, other_campaign = _campaign(world), _campaign(world)
    _live_session(world, campaign)
    account = _account(world)
    seat = _seat(world, campaign)
    other_seat = _seat(world, other_campaign)
    sheet = _document(world, campaign)
    group = _group(world, campaign, members=(seat,))
    leaving = _group(world, campaign, "Watch", (seat,))
    resolver = PrincipalResolver(world.db, _facts(world))

    def standing():
        return resolver.table(campaign, account=account, screen=None)

    with world.db.transaction() as unit:
        world.participants.offer(unit, other_campaign, other_seat, user_id=account.user_id)
        assert world.participants.accept(unit, other_campaign, other_seat, user_id=account.user_id)
    assert standing() is policy.TableRefusal.INACTIVE
    with world.db.transaction() as unit:
        world.participants.offer(unit, campaign, seat, user_id=account.user_id)
    assert standing() is policy.TableRefusal.INACTIVE  # user_id is set but accepted_at is still NULL
    with world.db.transaction() as unit:
        assert world.participants.accept(unit, campaign, seat, user_id=account.user_id)
        assert world.documents.link_character_sheet(unit, campaign, sheet, participant_id=seat)
        assert world.documents.set_archived(unit, campaign, sheet, archived=True)
    awaiting = standing()
    assert isinstance(awaiting, policy.TablePrincipal)
    assert (awaiting.kind, awaiting.participant_id, awaiting.scope) == (policy.TableKind.SEATED_AWAITING, seat, None)
    assert awaiting.characters == awaiting.groups == frozenset()
    assert policy.entitled(awaiting, seat) is policy.Entitlement.ABSENT
    with world.db.transaction() as unit:
        assert world.participants.confirm(unit, campaign, seat)

    confirmed = standing()
    assert isinstance(confirmed, policy.TablePrincipal)
    assert (confirmed.kind, confirmed.participant_id, confirmed.scope) == (
        policy.TableKind.SEATED_CONFIRMED, seat, seat
    )
    assert confirmed.characters == {sheet} and confirmed.groups == {group, leaving}
    assert policy.entitled(confirmed, seat) is policy.Entitlement.ENTITLED
    with world.db.transaction() as unit:
        unit.lock_campaign(campaign, shared=False)
        assert world.eligibility.remove_member(unit, campaign, leaving, seat)
        assert world.eligibility.remove_group(unit, campaign, group)
        assert world.documents.unlink_character_sheet(unit, campaign, sheet)
        unit.advance_authz_revision(campaign)
    updated = standing()
    assert isinstance(updated, policy.TablePrincipal)
    assert updated.characters == updated.groups == frozenset()
    assert updated.authz_revision == confirmed.authz_revision + 1

    # No reconciliation or revision change needs to finish before a seat is denied.
    with world.db.transaction() as unit:
        assert world.participants.remove(unit, campaign, seat)
    assert standing() is policy.TableRefusal.INACTIVE
    assert resolver.gm(campaign, account=SessionData(world.owner, "dm")).authz_revision == updated.authz_revision


def test_screen_precedence_revoke_rotation_and_end_are_reread(world: World) -> None:
    campaign, foreign = _campaign(world), _campaign(world)
    owner = SessionData(world.owner, "dm")
    resolver = PrincipalResolver(world.db, _facts(world))
    assert resolver.table(campaign, account=owner, screen=None) is policy.TableRefusal.INACTIVE
    session = _live_session(world, campaign)
    assert resolver.table(campaign, account=owner, screen=None, now=datetime.now(UTC) + timedelta(days=1)) is (
        policy.TableRefusal.INACTIVE
    )

    def mint():
        with world.db.transaction() as unit:
            stored, secret = world.sessions.mint_screen(unit, campaign, session, owner_id=owner.user_id)
        with world.db.transaction() as unit:
            screen = world.sessions.resolve_screen(unit, ident.digest(secret), now=datetime.now(UTC))
        assert screen is not None
        return stored, screen

    stored, screen = mint()
    view = resolver.table(campaign, account=owner, screen=screen)
    assert isinstance(view, policy.TablePrincipal)
    assert (view.kind, view.session_id, view.scope, view.grant_id) == (
        policy.TableKind.SCREEN, session, None, stored.id
    )
    assert resolver.table(foreign, account=owner, screen=screen) is policy.TableRefusal.INACTIVE
    with world.db.transaction() as unit:
        assert world.sessions.revoke_screen(unit, campaign, stored.id, owner_id=owner.user_id)
    assert resolver.table(campaign, account=None, screen=screen) is policy.TableRefusal.UNAUTHENTICATED
    fallback = resolver.table(campaign, account=owner, screen=screen)
    assert isinstance(fallback, policy.TablePrincipal) and fallback.kind is policy.TableKind.OWNER_VIEWER

    _, screen = mint()
    with world.db.transaction() as unit:
        assert world.sessions.rotate(
            unit, campaign, session, owner_id=owner.user_id, command_id=secrets.token_urlsafe(16)
        )
    assert resolver.table(campaign, account=None, screen=screen) is policy.TableRefusal.UNAUTHENTICATED
    _, current = mint()
    rotated = resolver.table(campaign, account=None, screen=current)
    assert isinstance(rotated, policy.TablePrincipal) and rotated.generation == view.generation + 1
    with world.db.transaction() as unit:
        assert world.sessions.end(unit, campaign, session, owner_id=owner.user_id)
    assert resolver.table(campaign, account=owner, screen=current) is policy.TableRefusal.INACTIVE


@pytest.mark.parametrize("world", ["fake"], indirect=True)
def test_twin_resolver_never_takes_a_lock_or_writes(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    campaign = _campaign(world)
    _live_session(world, campaign)
    resolver = PrincipalResolver(world.db, _facts(world))

    def fail(*_args, **_kwargs):
        pytest.fail("the resolver attempted a write or lock")

    with world.db.transaction() as unit:
        unit_type = type(unit)
    for name in ("lock_campaign", "advance_authz_revision", "note_row_lock", "lock", "notify", "claim_writer"):
        monkeypatch.setattr(unit_type, name, fail)
    owner = SessionData(world.owner, "dm")
    assert isinstance(resolver.table(campaign, account=owner, screen=None), policy.TablePrincipal)
    assert isinstance(resolver.gm(campaign, account=owner), policy.GmPrincipal)


@pytest.mark.parametrize("world", [pytest.param("postgres", marks=needs_db)], indirect=True)
def test_one_statement_and_no_locks_on_the_loaded_tables(world: World) -> None:
    campaign = _campaign(world)
    session = _live_session(world, campaign)
    account = _account(world)
    seat = _seat(world, campaign)
    sheet = _document(world, campaign)
    group = _group(world, campaign, members=(seat,))
    with world.db.transaction() as unit:
        world.participants.offer(unit, campaign, seat, user_id=account.user_id)
        assert world.participants.accept(unit, campaign, seat, user_id=account.user_id)
        assert world.participants.confirm(unit, campaign, seat)
        assert world.documents.link_character_sheet(unit, campaign, sheet, participant_id=seat)
        stored, _ = world.sessions.mint_screen(unit, campaign, session, owner_id=world.owner)
    # Lock setup uses fixed, fully qualified identifiers.
    lock_rows = [
        ("SELECT id FROM campaign.campaigns WHERE id = %s FOR UPDATE", campaign),
        ("SELECT campaign_id FROM campaign.authz_state WHERE campaign_id = %s FOR UPDATE", campaign),
        ("SELECT id FROM campaign.table_sessions WHERE id = %s FOR UPDATE", session),
        ("SELECT id FROM campaign.table_credentials WHERE id = %s FOR UPDATE", stored.id),
        ("SELECT id FROM campaign.participants WHERE id = %s FOR UPDATE", seat),
        ("SELECT id FROM campaign.documents WHERE id = %s FOR UPDATE", sheet),
        ("SELECT id FROM campaign.groups WHERE id = %s FOR UPDATE", group),
        ("SELECT group_id FROM campaign.group_members WHERE group_id = %s FOR UPDATE", group),
    ]
    facts = PostgresPrincipalFacts()
    with connect(world.dsn) as locked, connect(world.dsn) as reader, locked.transaction(), reader.transaction():
        for statement, value in lock_rows:
            assert locked.execute(statement, (value,)).fetchone() is not None
        reader.execute("SET LOCAL lock_timeout = '200ms'")
        proxy = _CountingConnection(reader)
        unit = PgTransaction(proxy)
        table = facts.table_facts(unit, campaign, user_id=account.user_id, grant_id=stored.id)
        assert proxy.statements == 1
        assert table.groups == {seat: {group}} and table.sheets == {seat: {sheet}}
        assert table.grant is not None and table.grant.id == stored.id
        gm = facts.gm_facts(unit, campaign, user_id=world.owner)
        assert proxy.statements == 2
        assert gm is not None and gm.owner_id == world.owner


@pytest.mark.parametrize("world", [pytest.param("postgres", marks=needs_db)], indirect=True)
def test_real_pool_errors_and_backend_commit_loss_fail_closed(world: World) -> None:
    campaign = _campaign(world)
    owner = SessionData(world.owner, "dm")
    limited = Database(world.dsn, PoolSettings(sync_max=1, async_max=0, acquire_timeout_s=1))
    resolver = PrincipalResolver(limited, PostgresPrincipalFacts())
    with limited.connection():
        _assert_unavailable(lambda: resolver.gm(campaign, account=owner))
    limited.close()
    _assert_unavailable(lambda: resolver.gm(campaign, account=owner))

    class KillBeforeCommit:
        @contextmanager
        def transaction(self):
            with world.db.transaction() as unit:
                yield unit
                with connect(world.dsn) as terminate:
                    row = terminate.execute(
                        "SELECT pg_terminate_backend(%s)", (unit.conn.info.backend_pid,)
                    ).fetchone()
                    assert row is not None and row[0]

    resolver = PrincipalResolver(KillBeforeCommit(), PostgresPrincipalFacts())
    with pytest.raises(PolicyUnavailable) as outage:
        resolver.gm(campaign, account=owner)
    assert outage.value.__context__ is None and outage.value.__cause__ is None
    _assert_unavailable(lambda: resolver.gm(campaign, account=owner))


@needs_db
def test_resolver_and_current_table_gate_match_independent_oracle(dsn: str) -> None:
    session_store, reveal = PostgresTableSessionStore(slot_clear=no_slots), PostgresRevealStore()
    compared = 0
    with connect(dsn) as conn:
        class ExistingConnection:
            @contextmanager
            def transaction(self):
                with conn.transaction():
                    yield PgTransaction(conn)

        resolver = PrincipalResolver(ExistingConnection(), PostgresPrincipalFacts(), clock=lambda: pdp.T0)

        def compare(state: State, fixture: WrittenWorld, requester: Requester, slot: Slot, label: str) -> None:
            # Exercise the same grant-resolution precedence and view gates as table_api.
            secret = fixture.screens.get(requester.screen_grant)
            account = (
                None if requester.account is None
                else SessionData(fixture.accounts.get(requester.account, -1), "player")
            )
            unit = PgTransaction(conn)
            screen = None if secret is None else session_store.resolve_screen(unit, ident.digest(secret), now=pdp.T0)
            result = resolver.table(fixture.campaign_id, account=account, screen=screen)
            other = None if slot.participant is None else fixture.id(ident.PARTICIPANT, slot.participant)
            assert pdp.kind_name(result) == table_principal(state, requester).name, label
            assert policy.entitled(result, other).name == entitled(state, requester, slot).name, label

            if screen is not None:
                view = reveal.view_for_screen(unit, fixture.campaign_id, screen.grant_id, now=pdp.T0)
            elif account is not None:
                view = reveal.view_for_account(unit, fixture.campaign_id, user_id=account.user_id, now=pdp.T0)
            else:
                view = None
            assert (view is not None) == isinstance(result, policy.TablePrincipal), label
            if view is not None:
                assert isinstance(result, policy.TablePrincipal)
                assert view.is_owner == (result.kind is policy.TableKind.OWNER_VIEWER), label
                assert view.own_slot == (result.kind is policy.TableKind.SEATED_CONFIRMED), label
                assert (view.participant_id, view.session_id, view.generation) == (
                    result.participant_id, result.session_id, result.generation
                ), label

        for seed in range(10**6, 10**6 + 200):
            state = gen_world(random.Random(seed), Release.WORKBENCH_V1 if seed % 3 == 0 else Release.ASSISTANT)
            fixture, rng = write(conn, state), random.Random(seed + 11)
            for _ in range(25):
                requester, slot = gen_entitlement_case(rng, state)
                compare(state, fixture, requester, slot, f"generated seed {seed}")
                compared += 1

        for session in ENT_SESSIONS:
            state = entitlement_state(session)
            fixture = write(conn, state)
            for principal, session_state, slot, _expected in ENTITLEMENT_MATRIX:
                if session_state == session:
                    compare(
                        state, fixture, entitlement_requester(principal), entitlement_slot(principal, slot),
                        f"matrix {session} {principal} {slot}",
                    )
    assert compared >= 5000
