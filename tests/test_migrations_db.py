"""
The ordered migration runner against a real PostgreSQL (1kg.1.5).

What no fake can prove: that a database which predates the ledger and a fresh one
end up with the *same* schema; that a failed migration really leaves nothing;
that two real runners starting together apply each file once; that a held lock
and a held table time out instead of hanging a deploy.

Requires DATABASE_URL (CI always sets it). Run from the repo root:
    DATABASE_URL=postgresql://... uv run python -m pytest tests/test_migrations_db.py -q
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid

import psycopg
import pytest
from _pg import connect, needs_db, throwaway_database

from service import migrations as mig
from service.migrations import (
    Migration,
    MigrationDriftError,
    MigrationFailed,
    MigrationLockTimeout,
    MigrationsPending,
    Mode,
)

pytestmark = needs_db

PACKAGED = mig.discover()


@pytest.fixture
def dsn():
    with throwaway_database("mig") as target:
        yield target


def _extra(version: int, name: str, sql: str) -> Migration:
    return Migration(version, name, f"{version:04d}_{name}.sql", sql, mig.checksum(sql))


def _ledger(dsn: str) -> list[tuple[int, str, str]]:
    with connect(dsn) as conn:
        return conn.execute("SELECT version, name, checksum FROM app.schema_migrations ORDER BY version").fetchall()


def _exists(dsn: str, relation: str) -> bool:
    with connect(dsn) as conn:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,)).fetchone()[0]


# ── Fresh and existing databases converge ────────────────────────────────────

#: Production as it stood before b8o.2 and before any foreign key: what the
#: oldest volume still in use looks like. Built by hand, because the point is
#: that the migrations — not this test — know how to bring it forward.
PRE_EXPANSION = """
CREATE SCHEMA chat;
CREATE TABLE chat.messages (
  id BIGSERIAL PRIMARY KEY, conversation_id TEXT NOT NULL, mode TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), content TEXT NOT NULL,
  suggestions JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX chat_messages_conv_created_idx ON chat.messages (conversation_id, created_at);
CREATE TABLE chat.attachments (
  id BIGSERIAL PRIMARY KEY, conversation_id TEXT NOT NULL, filename TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT '', extracted_text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX chat_attachments_conv_created_idx ON chat.attachments (conversation_id, created_at);
CREATE TABLE chat.conversations (
  conversation_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE SCHEMA auth;
CREATE TABLE auth.users (
  id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX users_email_lower_uidx ON auth.users (lower(email));
CREATE TABLE auth.invites (
  token TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'player' CHECK (role IN ('player', 'dm')),
  expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ,
  used_by BIGINT REFERENCES auth.users (id), revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX invites_created_idx ON auth.invites (created_at);

INSERT INTO auth.users (email, password_hash) VALUES ('gm@example.com', 'x');
INSERT INTO chat.conversations (conversation_id, user_id) VALUES ('owned', 1);
INSERT INTO chat.messages (conversation_id, mode, role, content) VALUES
  ('owned', 'sage', 'user', 'kept'),
  ('never-owned', 'sage', 'user', 'a row older than the ownership table');
"""

#: Every schema the migrations own. `campaign` and `audit` arrive with 0004 and
#: 0005; naming them here before they exist costs nothing and means a later file
#: cannot quietly leave one out of the convergence comparison.
SCHEMAS = "('chat', 'auth', 'app', 'campaign', 'audit', 'metering')"

COLUMNS = f"""
SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
  FROM information_schema.columns WHERE table_schema IN {SCHEMAS}
"""
#: By definition, not by name: 0001 deliberately leaves a fresh database with two
#: identically-defined CHECKs on chat.conversations (one inline, one named).
CONSTRAINTS = f"""
SELECT n.nspname, rel.relname, c.contype, pg_get_constraintdef(c.oid), c.convalidated
  FROM pg_constraint c
  JOIN pg_class rel ON rel.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE n.nspname IN {SCHEMAS}
"""
INDEXES = f"SELECT schemaname, tablename, indexdef FROM pg_indexes WHERE schemaname IN {SCHEMAS}"
#: A trigger is schema the catalog queries above cannot see, and 0004's
#: AFTER INSERT on campaigns is load-bearing (RQ-1). `tgisinternal` excludes the
#: referential-integrity triggers PostgreSQL makes for foreign keys: their names
#: embed an OID, so they differ between any two databases and would make every
#: comparison fail for a reason that is not drift.
TRIGGERS = f"""
SELECT n.nspname, rel.relname, t.tgname, pg_get_triggerdef(t.oid)
  FROM pg_trigger t
  JOIN pg_class rel ON rel.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = rel.relnamespace
 WHERE n.nspname IN {SCHEMAS} AND NOT t.tgisinternal
"""


def _shape(dsn: str) -> dict[str, set[tuple]]:
    with connect(dsn) as conn:
        return {
            "columns": set(conn.execute(COLUMNS).fetchall()),
            "constraints": set(conn.execute(CONSTRAINTS).fetchall()),
            "indexes": set(conn.execute(INDEXES).fetchall()),
            "triggers": set(conn.execute(TRIGGERS).fetchall()),
        }


def test_a_fresh_database_gets_every_migration_once(dsn):
    report = mig.migrate(dsn)
    assert report.applied == tuple(m.filename for m in PACKAGED)
    assert (report.current, report.state) == (len(PACKAGED), "current")
    assert _ledger(dsn) == [(m.version, m.name, m.checksum) for m in PACKAGED]
    for relation in (
        "chat.messages",
        "chat.conversations",
        "auth.users",
        "auth.invites",
        "app.jobs",
        "campaign.campaigns",
        "campaign.authz_state",
        "campaign.participants",
        "campaign.table_sessions",
        "campaign.table_credentials",
        "campaign.seat_offers",
        "campaign.seat_blocks",
        "campaign.reveal_disclosures",
        "campaign.reveal_slots",
        "campaign.groups",
        "campaign.group_members",
        "campaign.field_eligibility",
        "campaign.projection_queue",
    ):
        assert _exists(dsn, relation), f"{relation} was not created"
    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'campaign' AND table_name = 'authz_state' "
            "AND column_name = 'projection_revision'"
        ).fetchone() == ("bigint", "NO", "0"), "0019 adds projection_revision, NOT NULL DEFAULT 0"
    for retired in (
        "campaign.enrolment_codes",
        "campaign.device_credentials",
        "campaign.session_join_counters",
    ):
        assert not _exists(dsn, retired), f"{retired} outlived the migration that retired it"

    again = mig.migrate(dsn)
    assert again.applied == () and again.state == "current"


def test_a_pre_expansion_database_is_adopted_and_ends_up_identical_to_a_fresh_one(dsn):
    """The bead's first two criteria in one: every migration runs once over the
    old shape, its rows survive, and the result is the schema a fresh database
    gets — so there is one schema, however a database came to be."""
    with connect(dsn) as conn:
        conn.execute(PRE_EXPANSION)

    report = mig.migrate(dsn)
    assert report.applied == tuple(m.filename for m in PACKAGED)
    assert mig.migrate(dsn).applied == ()

    with connect(dsn) as conn:
        kept = conn.execute("SELECT conversation_id, content FROM chat.messages ORDER BY id").fetchall()
        assert [row[0] for row in kept] == ["owned", "never-owned"], "adoption must not touch existing rows"
        # The repaired constraint: deleting the account now forgets who spent an invite.
        action = conn.execute(
            "SELECT confdeltype FROM pg_constraint WHERE conname = 'invites_used_by_fkey'"
        ).fetchone()[0]
        assert action == "n"

    with throwaway_database("mig_fresh") as fresh:
        mig.migrate(fresh)
        adopted_shape, fresh_shape = _shape(dsn), _shape(fresh)
        for part in ("columns", "constraints", "indexes", "triggers"):
            assert adopted_shape[part] == fresh_shape[part], f"{part} differ between an adopted and a fresh database"
        assert _ledger(dsn) == _ledger(fresh)


def test_the_stores_and_the_admin_cli_check_the_schema_and_never_change_it(dsn):
    """`python -m service.admin_invites` runs from an operator's checkout, which is
    not the deployed image: applying whatever migrations it holds, as a side effect
    of listing invites, would put unreviewed DDL into production."""
    from service.auth_store import PostgresAuthStore

    with pytest.raises(MigrationsPending, match="python -m service.migrations migrate"):
        PostgresAuthStore(dsn).ensure_schema()
    assert not _exists(dsn, "app.schema_migrations") and not _exists(dsn, "auth.users")

    mig.migrate(dsn)
    PostgresAuthStore(dsn).ensure_schema()


# ── The campaign schema's own guarantees (1kg.2.1) ───────────────────────────

#: A well-formed minted id, written out so the test does not depend on the minter.
CAMPAIGN_ID = "cmp_" + "a" * 22


def _one_user(conn, email: str = "gm@example.com") -> int:
    return conn.execute(
        "INSERT INTO auth.users (email, password_hash) VALUES (%s, 'x') RETURNING id", (email,)
    ).fetchone()[0]


def test_a_campaign_inserted_by_raw_sql_still_gets_its_authorisation_row(dsn):
    """RQ-1, proved where no store can stand in for it: the AFTER INSERT trigger
    makes the `authz_state` row, so no route, script, fixture or later epic can
    leave a campaign without one. This is the case the in-memory twin cannot
    have — its only path to a campaign is the fake store's `create`, which
    inserts the row itself — so it is proved here against the database."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, %s)",
            (CAMPAIGN_ID, owner, "Nocturne"),
        )
        assert conn.execute(
            "SELECT authz_revision, lock_token FROM campaign.authz_state WHERE campaign_id = %s",
            (CAMPAIGN_ID,),
        ).fetchone() == (0, 0), "a raw insert must still leave a campaign authorisable"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("id", "campaign_1", id="no-prefix"),
        pytest.param("id", "cmp_short", id="too-few-bits"),
        pytest.param("name", "", id="empty-name"),
    ],
)
def test_the_database_refuses_a_campaign_row_the_application_would_never_mint(dsn, column, value):
    """The CHECK constraints of 0004 are generated from
    `service.campaign_identity.id_check_regex`; this is the database half of that
    agreement, and `service/tests/test_campaign_schema_sql.py` is the text half."""
    import psycopg

    mig.migrate(dsn)
    row = {"id": CAMPAIGN_ID, "name": "Nocturne"} | {column: value}
    with connect(dsn) as conn:
        owner = _one_user(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, %s)",
                (row["id"], owner, row["name"]),
            )


#: Every table 0004 hangs off a campaign that 0009 and 0016 kept, with the column that
#: reaches a user, and 0008's two document tables (1kg.5.1): a document hangs
#: off its campaign and a version off its document, both ON DELETE CASCADE.
CAMPAIGN_TABLES = (
    "campaign.campaigns",
    "campaign.authz_state",
    "campaign.participants",
    "campaign.table_sessions",
    "campaign.table_credentials",
    "campaign.documents",
    "campaign.document_versions",
    "campaign.seat_offers",
    "campaign.reveal_disclosures",
    "campaign.reveal_slots",
    "campaign.groups",
    "campaign.group_members",
    "campaign.field_eligibility",
    "campaign.projection_queue",
)
GROUP_ID = "grp_" + "a" * 22

#: A character sheet linked to the whole campaign's one participant.
DOCUMENT_ID = "doc_" + "a" * 22
PARTICIPANT_ID = "prt_" + "a" * 22


def _a_whole_campaign(conn, owner: int) -> None:
    """One row in every table of 0004 that 0009 and the table-session migration
    kept, of the seat migration's offers, and of the reveal migration's
    disclosures and slots (1kg.7.1), so the cascade has something to lose. The
    slot is the TABLE slot, which only the session's cascade reaches, and the
    disclosure is ended: a live copy would make deleting its document fail by
    design, which `tests/test_reveal_db.py` proves, together with an account's
    deletion taking a live copy with it."""
    conn.execute(
        "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
        (CAMPAIGN_ID, owner),
    )
    conn.execute(
        "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key) "
        "VALUES (%s, %s, 'Rook', 'rook')",
        ("prt_" + "a" * 22, CAMPAIGN_ID),
    )
    conn.execute(
        "INSERT INTO campaign.table_sessions (id, campaign_id, gm_user_id, state, expires_at) "
        "VALUES (%s, %s, %s, 'live', now() + interval '12 hours')",
        ("ses_" + "a" * 22, CAMPAIGN_ID, owner),
    )
    conn.execute(
        "INSERT INTO campaign.table_credentials "
        "(id, session_id, link_generation, credential_digest) VALUES (%s, %s, 1, %s)",
        ("tcr_" + "a" * 22, "ses_" + "a" * 22, "2" * 64),
    )
    conn.execute(
        "INSERT INTO campaign.documents (id, campaign_id, type, type_version, data, "
        "write_revision, field_revisions, name_key, search_key, linked_participant_id) "
        "VALUES (%s, %s, 'character-sheet', 1, %s::jsonb, 1, %s::jsonb, 'rook', 'rook', %s)",
        (DOCUMENT_ID, CAMPAIGN_ID, '{"name": "Rook"}', '{"name": 1}', PARTICIPANT_ID),
    )
    conn.execute(
        "INSERT INTO campaign.document_versions (document_id, number, author, summary, "
        "changed_fields, data, created_at, updated_at) "
        "VALUES (%s, 1, 'gm', '', %s::jsonb, %s::jsonb, now(), now())",
        (DOCUMENT_ID, '["name"]', '{"name": "Rook"}'),
    )
    conn.execute(
        "INSERT INTO campaign.seat_offers (id, campaign_id, participant_id, offered_by, address, address_key, "
        "expires_at) VALUES (%s, %s, %s, %s, 'wren@example.com', 'wren@example.com', now() + interval '14 days')",
        ("sof_" + "a" * 22, CAMPAIGN_ID, PARTICIPANT_ID, owner),
    )
    conn.execute(
        "INSERT INTO campaign.reveal_disclosures (id, campaign_id, session_id, document_id, version_number, "
        "mask, audience_kind, command_id, created_at, ended_at, ended_reason) "
        "VALUES (%s, %s, %s, %s, 1, ARRAY['name'], 'table', %s, now(), now(), 'gm_stop')",
        ("dsc_" + "a" * 22, CAMPAIGN_ID, "ses_" + "a" * 22, DOCUMENT_ID, "c" * 22),
    )
    conn.execute(
        "INSERT INTO campaign.reveal_slots (id, campaign_id, session_id, audience_kind, participant_id, seq, "
        "disclosure_id, updated_at) VALUES (%s, %s, %s, 'table', NULL, 2, NULL, now())",
        ("rsl_" + "a" * 22, CAMPAIGN_ID, "ses_" + "a" * 22),
    )
    _eligibility_rows(conn)


def _eligibility_rows(conn, campaign: str = CAMPAIGN_ID, document: str = DOCUMENT_ID) -> None:
    """0019's four tables, one row each: a group with the campaign's seat in it,
    a field classified for that group, and a queued projection of it."""
    conn.execute(
        "INSERT INTO campaign.groups (id, campaign_id, name, name_fold) VALUES (%s, %s, 'Scouts', 'scouts')",
        (GROUP_ID, campaign),
    )
    conn.execute(
        "INSERT INTO campaign.group_members (group_id, participant_id, campaign_id) VALUES (%s, %s, %s)",
        (GROUP_ID, PARTICIPANT_ID, campaign),
    )
    conn.execute(
        "INSERT INTO campaign.field_eligibility (document_id, field_key, campaign_id, eligibility_class, "
        "principal_ids, classification_source) VALUES (%s, 'portrait', %s, 'groups', %s, 'gm')",
        (document, campaign, [GROUP_ID]),
    )
    conn.execute(
        "INSERT INTO campaign.projection_queue (campaign_id, document_id, field_key, authz_revision) "
        "VALUES (%s, %s, 'portrait', 1)",
        (campaign, document),
    )


def test_deleting_a_user_cascades_through_every_table_of_the_campaign_schema(dsn):
    """SEC-36: deleting the account takes the campaigns with it, and everything
    that hangs off them — including the authorisation row, which is what an
    orphan would keep a deleted campaign authorisable by."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        _a_whole_campaign(conn, owner)
        for table in CAMPAIGN_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1, table

        conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))
        for table in CAMPAIGN_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, (
                f"{table} still holds a row of a deleted account"
            )


def test_audit_rows_outlive_the_campaign_they_name_and_its_owner(dsn):
    """ED-26 ("ledger and audit rows, which are tombstoned past campaign
    deletion") and ED-18(a)'s retention — NOT SEC-36, which says the opposite
    about everything that hangs off a campaign and which behaviour 33 pins.
    `campaign_id_tombstone` has no foreign key, so the row keeps the identifier
    as plain text and stops being reachable from what it names."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        _a_whole_campaign(conn, owner)
        conn.execute(
            "INSERT INTO audit.events "
            "(campaign_id_tombstone, actor_kind, action, object_kind, decision, detail) "
            "VALUES (%s, 'gm', 'session.started', 'table_session', 'allowed', %s)",
            (CAMPAIGN_ID, '{"generation": 1}'),
        )

        conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (CAMPAIGN_ID,))
        assert conn.execute("SELECT count(*) FROM audit.events").fetchone()[0] == 1

        conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))
        kept = conn.execute(
            "SELECT campaign_id_tombstone, action, detail FROM audit.events"
        ).fetchall()
        assert kept == [(CAMPAIGN_ID, "session.started", {"generation": 1})], (
            "the ledger must survive the account it describes"
        )


def test_the_audit_table_is_reachable_from_no_foreign_key(dsn):
    """The mechanism behind the test above, asserted directly: a foreign key
    added later would silently reintroduce the cascade ED-26 forbids."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        edges = conn.execute(
            "SELECT conname FROM pg_constraint c JOIN pg_class rel ON rel.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = rel.relnamespace "
            "WHERE n.nspname = 'audit' AND c.contype = 'f'"
        ).fetchall()
        assert edges == []


def test_deleting_one_document_takes_its_versions_and_its_link_and_leaves_the_ledger(dsn):
    """LIB-18's hard delete, at the database: the versions go by the foreign
    key's cascade, the character-sheet link goes with the row it is a column
    of, the participant it pointed at stays (AUD-16), and every audit row stays
    — including the one that names the deleted sheet, because the ledger is
    reachable from no foreign key and carries ids and codes only (ED-18(a),
    ED-26). That row is the only tombstone there is."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        _a_whole_campaign(conn, owner)
        detail = {"participant_id": PARTICIPANT_ID, "document_id": DOCUMENT_ID}
        conn.execute(
            "INSERT INTO audit.events "
            "(campaign_id_tombstone, actor_kind, action, object_kind, decision, detail) "
            "VALUES (%s, 'gm', 'participant.linked', 'participant', 'allowed', %s::jsonb)",
            (CAMPAIGN_ID, json.dumps(detail)),
        )

        conn.execute(
            "DELETE FROM campaign.documents WHERE id = %s AND campaign_id = %s",
            (DOCUMENT_ID, CAMPAIGN_ID),
        )

        for table in ("campaign.documents", "campaign.document_versions"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
        assert conn.execute(
            "SELECT count(*) FROM campaign.documents WHERE linked_participant_id = %s",
            (PARTICIPANT_ID,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT removed_at FROM campaign.participants WHERE id = %s", (PARTICIPANT_ID,)
        ).fetchall() == [(None,)], "the seat the sheet was linked to is untouched"
        kept = conn.execute("SELECT action, detail FROM audit.events").fetchall()
    assert kept == [("participant.linked", detail)]


# ── Conversation metadata, and the uncampaigned state (0006) ─────────────────


def test_a_conversation_written_before_the_migration_reads_back_uncampaigned(dsn):
    """RAIL-13. A NULL campaign_id is the documented uncampaigned state — every
    conversation that exists today, and plain chat from now on. It is not a
    migration that has yet to finish."""
    with connect(dsn) as conn:
        conn.execute(PRE_EXPANSION)

    mig.migrate(dsn)

    with connect(dsn) as conn:
        kept = conn.execute(
            "SELECT conversation_id, campaign_id, title, updated_at, archived_at "
            "FROM chat.conversations ORDER BY conversation_id"
        ).fetchall()
    assert kept == [("owned", None, None, None, None)], (
        "a conversation older than the campaign schema must still read back, uncampaigned"
    )


def test_deleting_the_campaign_owner_is_refused_while_another_users_conversation_links_to_it(dsn):
    """The cost of the second edge into chat.conversations, case (a): user V's
    conversation points at user U's campaign, so deleting U is refused. The edge
    is DEFERRABLE INITIALLY DEFERRED, so the refusal arrives at the COMMIT
    rather than at the statement — which is why this test commits explicitly
    instead of relying on autocommit to do it out of sight.

    Unconditional referential integrity, and the fail-closed answer requirement
    5 asks for: no conversation is silently detached or destroyed before 1kg.2.6
    decides the deletion order.
    """
    import psycopg

    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn, "gm@example.com")
        guest = _one_user(conn, "player@example.com")
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
            (CAMPAIGN_ID, owner),
        )
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id, campaign_id) "
            "VALUES ('theirs', %s, %s)",
            (guest, CAMPAIGN_ID),
        )

    with connect(dsn, autocommit=False) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))
            conn.commit()
        conn.rollback()

    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM campaign.campaigns").fetchone()[0] == 1
        assert conn.execute(
            "SELECT campaign_id FROM chat.conversations WHERE conversation_id = 'theirs'"
        ).fetchone()[0] == CAMPAIGN_ID, "nothing was detached on the way to the refusal"
        assert conn.execute("SELECT count(*) FROM auth.users WHERE id = %s", (owner,)).fetchone()[0] == 1


def test_deleting_an_owner_whose_own_conversation_links_to_their_campaign_deletes_both(dsn):
    """Case (b), which used to have no determinate answer and now has one.

    With an IMMEDIATE NO ACTION edge the check was an AFTER DELETE row trigger
    on campaign.campaigns queued at the end of the NESTED cascade query, so
    whether the owner's own conversations were already gone fell out of the
    firing order of two referential-integrity triggers on auth.users — and their
    names embed an OID rendered as text. CI's freshly initialised cluster
    cascaded; a long-lived cluster whose OID counter has six digits would have
    refused the very same delete.

    DEFERRABLE INITIALLY DEFERRED removes the question: the check runs at the
    end of the transaction, by which time the owner's conversations have gone
    with the user cascade and there is nothing left to check. The account goes,
    its campaign goes, its conversations go, and no row is left pointing at a
    campaign that is not there — on every cluster.
    """
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
            (CAMPAIGN_ID, owner),
        )
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id, campaign_id) "
            "VALUES ('mine', %s, %s)",
            (owner, CAMPAIGN_ID),
        )
        conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))

    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM chat.conversations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM campaign.campaigns").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM auth.users").fetchone()[0] == 0


def test_the_conversation_edge_is_deferred_and_still_takes_no_delete_action(dsn):
    """The mechanism behind the two tests above, asserted directly: a later
    migration that made the edge immediate again, or gave it a cascade, would
    reintroduce the OID-order dependency or silently destroy conversations."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        [(definition, deferrable, deferred)] = conn.execute(
            "SELECT pg_get_constraintdef(c.oid), c.condeferrable, c.condeferred "
            "FROM pg_constraint c JOIN pg_class rel ON rel.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = rel.relnamespace "
            "WHERE n.nspname = 'chat' AND rel.relname = 'conversations' "
            "AND c.contype = 'f' AND pg_get_constraintdef(c.oid) LIKE '%%campaign.campaigns%%'"
        ).fetchall()
    assert (deferrable, deferred) == (True, True), definition
    assert "ON DELETE" not in definition, "still NO ACTION: 1kg.2.6 decides the deletion order"


# ── Drift fails loudly ───────────────────────────────────────────────────────


def test_a_released_file_that_changed_stops_the_runner(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        conn.execute("UPDATE app.schema_migrations SET checksum = repeat('0', 64) WHERE version = 2")
    with pytest.raises(MigrationDriftError, match="0002_auth_schema.sql changed after it was applied"):
        mig.migrate(dsn)


def test_a_hole_in_the_history_stops_the_runner(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        conn.execute("DELETE FROM app.schema_migrations WHERE version = 2")
    with pytest.raises(MigrationDriftError, match="0002_auth_schema.sql is unapplied, below version"):
        mig.migrate(dsn)


def test_a_database_ahead_of_this_build_is_served(dsn):
    """An older image during a rollout, or after a rollback: not an error."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO app.schema_migrations (version, name, checksum, duration_ms, applied_by) "
            "VALUES (99, 'from_a_newer_build', repeat('a', 64), 1, 'newer')"
        )
    report = mig.migrate(dsn)
    assert (report.state, report.ahead, report.current, report.applied) == ("ahead", (99,), 99, ())


def test_verify_mode_creates_nothing(dsn):
    with pytest.raises(MigrationsPending, match="0001_chat_schema.sql"):
        mig.migrate(dsn, mode=Mode.VERIFY)
    assert not _exists(dsn, "app.schema_migrations") and not _exists(dsn, "chat.messages")
    mig.migrate(dsn)
    assert mig.migrate(dsn, mode=Mode.VERIFY).state == "current"


# ── A failed migration leaves nothing ────────────────────────────────────────


def test_a_failed_migration_rolls_back_whole_and_can_be_retried(dsn):
    half = _extra(len(PACKAGED) + 1, "half_done", "CREATE TABLE app.half_done (id int);\nSELECT 1/0;")
    with pytest.raises(MigrationFailed, match="half_done.sql failed and was rolled back .*SQLSTATE 22012"):
        mig.migrate(dsn, packaged=[*PACKAGED, half])

    assert not _exists(dsn, "app.half_done"), "DDL before the failure must be gone too"
    assert [row[0] for row in _ledger(dsn)] == [m.version for m in PACKAGED], "what came before stays applied"

    fixed = _extra(half.version, "half_done", "CREATE TABLE app.half_done (id int);")
    assert mig.migrate(dsn, packaged=[*PACKAGED, fixed]).applied == (fixed.filename,)
    assert _exists(dsn, "app.half_done")


def test_a_failure_never_quotes_row_values(dsn):
    """PostgreSQL puts the offending values in DETAIL. A data migration that
    trips a constraint must not put a user's email in the deploy log."""
    leaky = _extra(
        len(PACKAGED) + 1,
        "leaky",
        "CREATE TABLE app.leaky (email text UNIQUE);\n"
        "INSERT INTO app.leaky VALUES ('gm@example.com'), ('gm@example.com');",
    )
    with pytest.raises(MigrationFailed) as caught:
        mig.migrate(dsn, packaged=[*PACKAGED, leaky])
    assert "SQLSTATE 23505" in str(caught.value)
    assert "gm@example.com" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_a_conversion_failure_does_not_quote_the_value_it_choked_on(dsn):
    """Class 22 puts the datum in the PRIMARY message, not in DETAIL."""
    tighten = _extra(
        len(PACKAGED) + 1,
        "tighten",
        "CREATE TABLE app.handles (handle text);\n"
        "INSERT INTO app.handles VALUES ('Seraphine, the hooded stranger');\n"
        "ALTER TABLE app.handles ALTER COLUMN handle TYPE integer USING handle::integer;",
    )
    with pytest.raises(MigrationFailed) as caught:
        mig.migrate(dsn, packaged=[*PACKAGED, tighten])
    assert "SQLSTATE 22P02" in str(caught.value)
    assert "Seraphine" not in str(caught.value)
    assert not _exists(dsn, "app.handles")


def test_a_file_that_commits_by_itself_never_gets_a_ledger_row(dsn):
    """`END;` is SQL for COMMIT and cannot be banned textually (PL/pgSQL uses it),
    so the runner asks the server whether it is still inside its transaction.
    What the file did before its COMMIT is permanent — this pins that honestly —
    but it is never recorded as a clean apply, and the failure says what to do."""
    sneaky = _extra(
        len(PACKAGED) + 1,
        "sneaky",
        "CREATE TABLE app.before_commit (id int);\nEND;\nCREATE TABLE app.after_commit (id int);",
    )
    with pytest.raises(MigrationFailed, match="sneaky.sql ended the runner's transaction"):
        mig.migrate(dsn, packaged=[*PACKAGED, sneaky])
    assert [row[0] for row in _ledger(dsn)] == [m.version for m in PACKAGED]
    assert _exists(dsn, "app.before_commit"), "committed by the file itself; the message says to repair by hand"


def test_a_migration_that_cannot_get_its_table_lock_gives_up(dsn, monkeypatch):
    """Rather than queueing in front of every other statement on that table."""
    mig.migrate(dsn)
    monkeypatch.setattr(mig, "LOCK_TIMEOUT_S", 1)
    blocked = _extra(len(PACKAGED) + 1, "blocked", "ALTER TABLE chat.messages ADD COLUMN blocked int;")
    with connect(dsn, autocommit=False) as holder:
        holder.execute("LOCK TABLE chat.messages IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        with pytest.raises(MigrationFailed, match="SQLSTATE 55P03"):
            mig.migrate(dsn, packaged=[*PACKAGED, blocked])
        assert time.monotonic() - started < 8
        holder.rollback()
    assert mig.migrate(dsn, packaged=[*PACKAGED, blocked]).applied == (blocked.filename,)


def test_a_role_that_may_not_create_the_ledger_is_a_verdict_not_an_outage(dsn):
    """Startup degrades around an unreachable database. A database that answers
    "no" will answer it again at every start, so the process must not come up
    half-working on it."""
    from psycopg.conninfo import make_conninfo

    role = f"mig_limited_{uuid.uuid4().hex[:8]}"
    with connect(dsn) as admin:
        admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD 'limited_not_a_secret'")
    try:
        limited = make_conninfo(dsn, user=role, password="limited_not_a_secret")
        with pytest.raises(MigrationFailed, match="refused the migration ledger .*SQLSTATE 42501"):
            mig.migrate(limited)
    finally:
        with connect(dsn) as admin:
            admin.execute(f"DROP ROLE {role}")


# ── Concurrent startups ──────────────────────────────────────────────────────


def test_two_instances_starting_together_apply_each_migration_once(dsn):
    """Without the advisory lock the second runner would run 0001 alongside the
    first, and the slow marker below would fail with "already exists"."""
    slow = _extra(len(PACKAGED) + 1, "slow", "SELECT pg_sleep(1.5);\nCREATE TABLE app.slow_marker (id int);")
    packaged = [*PACKAGED, slow]
    barrier = threading.Barrier(2)
    reports: list[mig.Report] = []
    errors: list[str] = []

    def start_instance() -> None:
        try:
            barrier.wait(10)
            reports.append(mig.migrate(dsn, packaged=packaged))
        except Exception as exc:  # the type only: a driver message could name the DSN
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=start_instance) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert errors == []
    assert sorted(len(r.applied) for r in reports) == [0, len(packaged)], "one did everything, the other nothing"
    assert [row[0] for row in _ledger(dsn)] == [m.version for m in packaged]


def test_a_lock_held_too_long_times_out_instead_of_hanging_the_deploy(dsn):
    with connect(dsn) as holder:
        holder.execute("SELECT pg_advisory_lock(%s, 0)", (int(mig.AdvisoryLock.MIGRATIONS),))
        started = time.monotonic()
        with pytest.raises(MigrationLockTimeout):
            mig.migrate(dsn, lock_wait_s=1.0)
        assert time.monotonic() - started < 8
    assert not _exists(dsn, "chat.messages")


def test_a_current_database_never_waits_for_the_lock(dsn):
    """A cold start during someone else's long migration run must not queue:
    nothing is pending for it, so it takes no lock at all."""
    mig.migrate(dsn)
    with connect(dsn) as holder:
        holder.execute("SELECT pg_advisory_lock(%s, 0)", (int(mig.AdvisoryLock.MIGRATIONS),))
        started = time.monotonic()
        assert mig.migrate(dsn, lock_wait_s=30.0).state == "current"
        assert time.monotonic() - started < 5


# ── The operator's CLI ───────────────────────────────────────────────────────


def test_the_cli_reports_pending_then_applies_then_reports_current(dsn, monkeypatch, capsys):
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", dsn)
    assert mig.main(["status"]) == 1
    assert mig.main(["migrate"]) == 0
    assert mig.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "applied 0001_chat_schema.sql" in out and dsn not in out


# ── The channel a conversation was started in, and the owner's index (0007) ──


def test_a_conversation_written_before_the_migration_has_no_recorded_channel(dsn):
    """`started_mode` is NULL for every conversation that exists today, and that
    is the honest value: the channel it was started in was never recorded, so it
    is unknown rather than absent. Nothing backfills it and nothing guesses it."""
    with connect(dsn) as conn:
        conn.execute(PRE_EXPANSION)

    mig.migrate(dsn)

    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT started_mode FROM chat.conversations ORDER BY conversation_id"
        ).fetchall() == [(None,)]


@pytest.mark.parametrize("mode", ["sage", "spell", "rules", "gm"])
def test_the_database_accepts_every_channel_the_enum_carries(dsn, mode):
    """The database half of the agreement
    `service/tests/test_conversation_schema_sql.py` holds as text: a mode the
    enum names must be a mode the column stores, or the application has a
    channel it can never write."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id, started_mode) "
            "VALUES (%s, %s, %s)",
            (f"cnv_{mode}", owner, mode),
        )
        assert conn.execute(
            "SELECT started_mode FROM chat.conversations WHERE conversation_id = %s",
            (f"cnv_{mode}",),
        ).fetchone() == (mode,)


def test_the_database_refuses_a_channel_the_application_could_never_mean(dsn):
    """The other direction. `chat.messages.mode` carries no CHECK because a
    turn's mode is rewritten on every request; this column is bound once, so the
    constraint costs nothing and catches a typo at the statement."""
    import psycopg

    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO chat.conversations (conversation_id, user_id, started_mode) "
                "VALUES ('cnv_bad', %s, 'oracle')",
                (owner,),
            )


def test_a_conversation_id_is_still_unconstrained_so_every_existing_id_keeps_working(dsn):
    """Requirement 2's compatibility rule, proved against the server: the ids in
    production are client-minted UUIDs and the column must never gain a prefix
    CHECK that would make them illegal."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        legacy = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO chat.conversations (conversation_id, user_id) VALUES (%s, %s)",
            (legacy, owner),
        )
        assert conn.execute(
            "SELECT count(*) FROM chat.conversations WHERE conversation_id = %s", (legacy,)
        ).fetchone() == (1,)


def test_the_owner_index_exists_by_name_and_is_partial_on_the_default_filter(dsn):
    """A15. `ConversationStore.list_for_owner`'s ordering and default filter are
    written to match this index exactly, so an index quietly renamed, widened or
    reordered turns the default page into a sequential scan without failing
    anything else."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        definition = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'chat' AND indexname = 'conversations_owner_recent_idx'"
        ).fetchone()
    assert definition is not None, "conversations_owner_recent_idx is gone or was renamed"
    # Read through PostgreSQL's own rendering rather than matching it verbatim:
    # `pg_get_indexdef` normalises whitespace, casing and the parentheses around
    # an expression key, and a test pinned to one spelling of those would go red
    # for a reason that is not drift. Order and direction are what matter, and
    # both are still asserted.
    created = re.sub(r"\s+", " ", definition[0])
    assert "archived_at IS NULL" in created, created
    keys = created.split("USING btree (", 1)[1]
    assert keys.index("user_id") < keys.index("updated_at") < keys.index("conversation_id"), keys
    assert "COALESCE" in keys and keys.count("DESC") == 2, keys


# ── The seat confirmation and offers (bead 1kg.2.2) ──────────────────────────

#: Found by name, so the lead's renumbering at merge is an edit elsewhere.
SEAT_OFFERS = next(m for m in PACKAGED if m.name == "seat_confirmation_and_offers")


def test_the_seat_migration_adopts_an_accepted_seat_as_not_confirmed(dsn):
    """0012's CHECK is safe because `confirmed_at` is new: a seat the previous
    build accepted survives the expansion, and reads as not confirmed."""
    before = tuple(m for m in PACKAGED if m.version < SEAT_OFFERS.version)
    # Up to and including 0012, so that a later migration (0013, bead cfx)
    # cannot be the one this run applies last.
    through = tuple(m for m in PACKAGED if m.version <= SEAT_OFFERS.version)
    mig.migrate(dsn, packaged=before)
    with connect(dsn) as conn:
        owner, player = _one_user(conn), _one_user(conn, "wren@example.com")
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
        conn.execute(
            "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key, user_id, accepted_at) "
            "VALUES (%s, %s, 'Rook', 'rook', %s, now())",
            (PARTICIPANT_ID, CAMPAIGN_ID, player),
        )
    # Exactly the seat migration: a later one is not the expansion under test,
    # so it neither runs here nor stands in the report.
    assert mig.migrate(dsn, packaged=through).applied == (SEAT_OFFERS.filename,)
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT accepted_at IS NOT NULL, confirmed_at FROM campaign.participants WHERE id = %s",
            (PARTICIPANT_ID,),
        ).fetchone()
        assert row == (True, None)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key, confirmed_at) "
                "VALUES (%s, %s, 'Wren', 'wren', now())",
                ("prt_" + "b" * 22, CAMPAIGN_ID),
            )


#: Found by name, like SEAT_OFFERS, so a renumbering at merge is an edit elsewhere.
CAMPAIGN_SUMMARY = next(m for m in PACKAGED if m.name == "campaign_summary")


def test_the_card_migration_adopts_an_existing_campaign_as_untoned_5e_and_not_concluded(dsn):
    """0013 (bead cfx) adds three columns to a table that already holds rows,
    one NOT NULL: a campaign the previous build made survives the expansion
    with no tone line, as D&D 5e, and not concluded."""
    before = tuple(m for m in PACKAGED if m.version < CAMPAIGN_SUMMARY.version)
    through = tuple(m for m in PACKAGED if m.version <= CAMPAIGN_SUMMARY.version)
    mig.migrate(dsn, packaged=before)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
    assert mig.migrate(dsn, packaged=through).applied[-1] == CAMPAIGN_SUMMARY.filename
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT tone, game_system, concluded_at FROM campaign.campaigns WHERE id = %s", (CAMPAIGN_ID,)
        ).fetchone()
        assert row == (None, "dnd5e", None)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("address_key", "Wren@example.com"),
        ("address", "a@"),
        ("outcome", "ignored"),
        ("id", "sof_short"),
    ],
)
def test_the_database_refuses_an_offer_row_the_application_would_never_write(dsn, column, value):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        _a_whole_campaign(conn, owner)
        conn.execute("UPDATE campaign.seat_offers SET outcome = 'withdrawn', answered_at = now()")
        row = {
            "id": "sof_" + "b" * 22,
            "address": "wren@example.com",
            "address_key": "wren@example.com",
            "outcome": None,
        }
        row[column] = value
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.seat_offers (id, campaign_id, participant_id, offered_by, address, "
                "address_key, outcome, answered_at, expires_at) VALUES "
                "(%(id)s, %(campaign)s, %(seat)s, %(owner)s, %(address)s, %(address_key)s, %(outcome)s, "
                "CASE WHEN %(outcome)s::text IS NULL THEN NULL ELSE now() END, now() + interval '1 day')",
                {**row, "campaign": CAMPAIGN_ID, "seat": PARTICIPANT_ID, "owner": owner},
            )


def test_a_seat_holds_one_open_offer_by_the_partial_unique_index(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        _a_whole_campaign(conn, owner)
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO campaign.seat_offers (id, campaign_id, participant_id, offered_by, address, "
                "address_key, expires_at) VALUES (%s, %s, %s, %s, 'finch@example.com', 'finch@example.com', "
                "now() + interval '1 day')",
                ("sof_" + "b" * 22, CAMPAIGN_ID, PARTICIPANT_ID, owner),
            )


# ── Media assets and their quota (agent-forge-harness-1kg.8.1.1, AC-3) ──────
#
# Found by name, never by number: the lead renumbers at merge (R-7).
[MEDIA_MIGRATION] = [m for m in PACKAGED if m.filename.endswith("_media_assets.sql")]
BEFORE_MEDIA = [m for m in PACKAGED if m.version < MEDIA_MIGRATION.version]


def _usage(conn, campaign_id: str = CAMPAIGN_ID) -> tuple | None:
    return conn.execute(
        "SELECT bytes_reserved, asset_count FROM campaign.media_usage WHERE campaign_id = %s", (campaign_id,)
    ).fetchone()


def test_a_campaign_that_existed_before_the_media_migration_gets_a_usage_row_at_zero(dsn):
    """The backfill: the quota's conditional UPDATE assumes the row exists."""
    assert BEFORE_MEDIA and BEFORE_MEDIA[-1].version == MEDIA_MIGRATION.version - 1
    mig.migrate(dsn, packaged=BEFORE_MEDIA)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
        assert not _exists(dsn, "campaign.media_usage")
    assert mig.migrate(dsn).applied[0] == MEDIA_MIGRATION.filename
    with connect(dsn) as conn:
        assert _usage(conn) == (0, 0)


def test_a_campaign_inserted_by_raw_sql_gets_its_usage_row_from_the_trigger(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
        assert _usage(conn) == (0, 0)


def _asset_row(kind: str = "image", state: str = "uploading", n: int = 1, **overrides) -> dict:
    """One row of `campaign.assets` that every CHECK accepts, for `kind` in `state`."""
    image, ready = kind == "image", state == "ready"
    return {
        "id": "ast_" + f"{n:022d}",
        "campaign_id": CAMPAIGN_ID,
        "kind": kind,
        "state": state,
        "failure": "too_large" if state == "failed" else None,
        "declared_media_type": "image/png" if image else "audio/ogg",
        "declared_size_bytes": 1000,
        "media_type": ("image/png" if image else "audio/mpeg") if ready else None,
        "size_bytes": 900 if ready else None,
        "width": 40 if image and ready else None,
        "height": 30 if image and ready else None,
        "duration_ms": 12000 if not image and ready else None,
        "alt": "A red door" if image and state != "deleted" else None,
        "object_key": f"assets/{n:032x}",
        "tmp_key": f"tmp/{n:032x}",
    } | overrides


def _insert_asset(conn, row: dict) -> None:
    columns = ", ".join(row)
    conn.execute(
        f"INSERT INTO campaign.assets ({columns}) VALUES ({', '.join(f'%({c})s' for c in row)})", row
    )


def _a_campaign_with_its_owner(conn) -> int:
    owner = _one_user(conn)
    conn.execute(
        "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
    )
    return owner


FORBIDDEN_ASSETS = [
    ("a tombstone that carries alt", "image", "deleted", {"alt": "A red door"}),
    ("a tombstone that carries a measured value", "image", "deleted", {"size_bytes": 10}),
    ("a failed row with no failure", "image", "failed", {"failure": None}),
    ("a failed row that carries a measured value", "image", "failed", {"media_type": "image/png"}),
    ("a ready image with no dimensions", "image", "ready", {"width": None, "height": None}),
    ("a ready image with only one dimension", "image", "ready", {"height": None}),
    ("a ready image that carries a duration", "image", "ready", {"duration_ms": 1000}),
    ("a ready audio row that carries a width", "audio", "ready", {"width": 10}),
    ("a ready audio row that carries a height", "audio", "ready", {"height": 10}),
    ("a ready audio row that is not mp3", "audio", "ready", {"media_type": "audio/ogg"}),
    ("a ready image whose type is not its declared one", "image", "ready", {"media_type": "image/jpeg"}),
    ("an uploading row that carries a size", "image", "uploading", {"size_bytes": 10}),
    ("a malformed object key", "image", "uploading", {"object_key": "assets/" + "A" * 32}),
    ("a malformed tmp key", "image", "uploading", {"tmp_key": "tmp/../" + "0" * 29}),
    ("a failure on a row that is not failed", "image", "uploading", {"failure": "timed_out"}),
    ("an audio row with alt text", "audio", "uploading", {"alt": "A red door"}),
    ("an image with no alt text", "image", "processing", {"alt": None}),
    ("a declared type its kind refuses", "image", "uploading", {"declared_media_type": "image/gif"}),
    ("a declared size over the cap", "audio", "uploading", {"declared_size_bytes": 20000001}),
    ("an image over the pixel cap", "image", "ready", {"width": 8192, "height": 8192}),
    ("a state the schema does not know", "image", "uploading", {"state": "archived"}),
    ("an id of another kind", "image", "uploading", {"id": "doc_" + "a" * 22}),
    ("a command id that is not one", "image", "uploading", {"created_command_id": "short"}),
]


@pytest.mark.parametrize(
    ("label", "kind", "state", "overrides"), FORBIDDEN_ASSETS, ids=[f[0] for f in FORBIDDEN_ASSETS]
)
def test_the_database_refuses_each_asset_row_the_schema_forbids(dsn, label, kind, state, overrides):
    import psycopg

    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_campaign_with_its_owner(conn)
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_asset(conn, _asset_row(kind, state) | overrides)


def test_the_database_accepts_one_valid_asset_row_of_each_kind_in_each_state(dsn):
    """So the CHECKs are shown to refuse only what L-3 forbids."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_campaign_with_its_owner(conn)
        n = 0
        for kind in ("image", "audio"):
            for state in ("uploading", "processing", "ready", "failed", "deleted"):
                n += 1
                _insert_asset(conn, _asset_row(kind, state, n))
        assert conn.execute("SELECT count(*) FROM campaign.assets").fetchone()[0] == 10


def test_the_database_refuses_a_duplicate_key(dsn):
    import psycopg

    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_campaign_with_its_owner(conn)
        _insert_asset(conn, _asset_row(n=1))
        for clash in ({"object_key": f"assets/{1:032x}"}, {"tmp_key": f"tmp/{1:032x}"}):
            with pytest.raises(psycopg.errors.UniqueViolation):
                _insert_asset(conn, _asset_row(n=2, **clash))
        _insert_asset(conn, _asset_row(n=3, created_command_id="c" * 16))
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_asset(conn, _asset_row(n=4, created_command_id="c" * 16))


@pytest.mark.parametrize("through", ["campaign", "owner"])
def test_a_campaign_that_holds_an_asset_row_cannot_be_deleted(dsn, through):
    """L-3(b): NO ACTION. Neither a direct DELETE nor the account's cascade may
    remove the rows and leave their bytes with nothing naming them."""
    import psycopg

    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _a_campaign_with_its_owner(conn)
        _insert_asset(conn, _asset_row(state="deleted"))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            if through == "campaign":
                conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (CAMPAIGN_ID,))
            else:
                conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))
        assert conn.execute("SELECT count(*) FROM campaign.assets").fetchone()[0] == 1


@pytest.mark.parametrize("through", ["campaign", "owner"])
def test_the_deletion_succeeds_after_the_primitive_in_the_same_transaction(dsn, through):
    from service.asset_store import PostgresAssetStore
    from service.db import CampaignLockSettings, Database, PoolSettings
    from service.jobs import PostgresJobQueue

    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _a_campaign_with_its_owner(conn)
        _insert_asset(conn, _asset_row(state="ready", n=1))
        _insert_asset(conn, _asset_row(state="deleted", n=2))
        conn.execute("UPDATE campaign.media_usage SET bytes_reserved = 900, asset_count = 1")
    db = Database(dsn, PoolSettings(sync_max=2, async_max=0), CampaignLockSettings(lock_timeout_s=1))
    store = PostgresAssetStore(PostgresJobQueue(db))
    with db.transaction() as unit:
        jobs = store.delete_campaign_assets(unit, CAMPAIGN_ID, owner_id=owner)
        if through == "campaign":
            unit.conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (CAMPAIGN_ID,))
        else:
            unit.conn.execute("DELETE FROM auth.users WHERE id = %s", (owner,))
    assert len(jobs) == 1, "one job for the live row; the tombstone already had its own"
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM campaign.campaigns").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM campaign.assets").fetchone()[0] == 0
        assert conn.execute("SELECT kind FROM app.jobs").fetchall() == [("asset.delete",)]


def test_media_usage_cascades_with_its_campaign(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_campaign_with_its_owner(conn)
        assert _usage(conn) == (0, 0)
        conn.execute("DELETE FROM campaign.campaigns WHERE id = %s", (CAMPAIGN_ID,))
        assert _usage(conn) is None


# ── 0016: the table session without a link or a join (1kg.2.3) ──────────────

#: Found by name, like 1kg.2.2's `SEAT_OFFERS`: every migration before it.
SESSION_ACCESS = next(m for m in PACKAGED if m.name == "table_session_access")
_BEFORE_SESSION_ACCESS = tuple(m for m in PACKAGED if m.version < SESSION_ACCESS.version)


def _actor_kind_check(dsn: str) -> list[tuple[str, str]]:
    """`(name, definition)` of every CHECK on `audit.events` that reads
    `actor_kind`, as the server has it."""
    with connect(dsn) as conn:
        return conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'audit.events'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%%actor_kind%%'"
        ).fetchall()


@needs_db
def test_the_actor_kind_check_0016_replaces_is_the_one_0005_created(dsn):
    """0016 drops a constraint by name, so the name is read off a database
    migrated to 0015 rather than assumed — and after 0016 there is still exactly
    one such CHECK, under the same name, with `screen` where `guest` was."""
    mig.migrate(dsn, packaged=_BEFORE_SESSION_ACCESS)
    [(name, definition)] = _actor_kind_check(dsn)
    assert name == "events_actor_kind_check" and "guest" in definition

    mig.migrate(dsn)
    [(name, definition)] = _actor_kind_check(dsn)
    assert name == "events_actor_kind_check"
    assert "screen" in definition and "guest" not in definition


@needs_db
def test_a_fresh_database_keeps_no_link_digest_and_one_digest_column_in_the_campaign_schema(dsn):
    """SEC-48: the screen grant's digest is the only secret's digest left in
    the campaign schema; the link's is gone with its index, and so is the
    join counter nothing ever wrote."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        digests = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'campaign' AND column_name LIKE '%%digest' "
            "ORDER BY table_name, column_name"
        ).fetchall()
        commands = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'campaign' AND table_name = 'table_sessions' "
            "AND column_name LIKE '%%command_id' ORDER BY column_name"
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'campaign' "
            "AND tablename = 'table_sessions' ORDER BY indexname"
        ).fetchall()
    assert digests == [("table_credentials", "credential_digest")]
    assert commands == [("rotate_command_id",), ("start_command_id",)]
    assert ("table_sessions_link_digest_uidx",) not in indexes
    assert ("table_sessions_start_command_uidx",) in indexes
    assert not _exists(dsn, "campaign.session_join_counters")


def _an_audit_row(conn, actor_kind: str) -> None:
    conn.execute(
        "INSERT INTO audit.events (campaign_id_tombstone, actor_kind, action, object_kind, "
        "decision) VALUES (%s, %s, 'screen.revoked', 'table_screen', 'allowed')",
        (CAMPAIGN_ID, actor_kind),
    )


@needs_db
def test_the_ledger_refuses_a_guest_and_accepts_a_screen(dsn):
    """D-1: there are no guests. A table screen acts only to leave (SEC-38)."""
    import psycopg

    mig.migrate(dsn)
    with connect(dsn) as conn:
        _an_audit_row(conn, "screen")
        with pytest.raises(psycopg.errors.CheckViolation):
            _an_audit_row(conn, "guest")
        assert conn.execute("SELECT count(*) FROM audit.events").fetchone()[0] == 1


@needs_db
def test_a_command_id_column_holds_the_contracts_shape_or_nothing(dsn):
    """0016's two CHECKs, and the start index: one session per command per
    campaign, while any number of sessions have none."""
    import psycopg

    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')",
            (CAMPAIGN_ID, owner),
        )

        def session(number: int, command: str | None, *, state: str = "ended") -> None:
            conn.execute(
                "INSERT INTO campaign.table_sessions (id, campaign_id, gm_user_id, state, "
                "expires_at, ended_at, start_command_id) VALUES (%s, %s, %s, %s, "
                "now() + interval '12 hours', CASE WHEN %s = 'live' THEN NULL ELSE now() END, %s)",
                ("ses_" + str(number) * 22, CAMPAIGN_ID, owner, state, state, command),
            )

        session(1, None)
        session(2, None)
        session(3, "A" * 16)
        with pytest.raises(psycopg.errors.UniqueViolation):
            session(4, "A" * 16)
        with pytest.raises(psycopg.errors.CheckViolation):
            session(5, "too-short")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE campaign.table_sessions SET rotate_command_id = 'has spaces in it ok' "
                "WHERE id = %s",
                ("ses_" + "1" * 22,),
            )


# ── Field eligibility, groups and the projection revision (0019, 1ir.2.1) ────

#: Found by name, like SEAT_OFFERS, so a renumbering at merge is an edit elsewhere.
ELIGIBILITY = next(m for m in PACKAGED if m.name == "field_eligibility_and_groups")
_BEFORE_ELIGIBILITY = tuple(m for m in PACKAGED if m.version < ELIGIBILITY.version)
_THROUGH_ELIGIBILITY = tuple(m for m in PACKAGED if m.version <= ELIGIBILITY.version)
OTHER_CAMPAIGN_ID = "cmp_" + "b" * 22
OTHER_DOCUMENT_ID = "doc_" + "b" * 22
OTHER_PARTICIPANT_ID = "prt_" + "b" * 22


def _two_campaigns(conn) -> None:
    """Campaign A with the whole campaign (its seat, sheet and 0019 rows), and
    campaign B with a seat and a sheet of its own and nothing classified."""
    owner = _one_user(conn)
    _a_whole_campaign(conn, owner)
    conn.execute(
        "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Other')", (OTHER_CAMPAIGN_ID, owner)
    )
    conn.execute(
        "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key) VALUES (%s, %s, 'Wren', 'wren')",
        (OTHER_PARTICIPANT_ID, OTHER_CAMPAIGN_ID),
    )
    conn.execute(
        "INSERT INTO campaign.documents (id, campaign_id, type, type_version, data, write_revision, "
        "field_revisions, name_key, search_key) VALUES (%s, %s, 'character-sheet', 1, %s::jsonb, 1, "
        "%s::jsonb, 'wren', 'wren')",
        (OTHER_DOCUMENT_ID, OTHER_CAMPAIGN_ID, '{"name": "Wren"}', '{"name": 1}'),
    )


def test_a_database_upgraded_across_the_eligibility_migration_converges_on_a_fresh_one(dsn):
    """T-A2: an existing campaign keeps its revision and reads projection 0 —
    'revision ahead, queue empty', no backfill (I-6) — its seat and sheet are
    untouched, the shape equals a fresh database's, and a second run applies
    nothing. Kills a backfill (M-A2) and a nullable or default-less column
    (M-A3)."""
    mig.migrate(dsn, packaged=_BEFORE_ELIGIBILITY)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
        conn.execute("UPDATE campaign.authz_state SET authz_revision = 3 WHERE campaign_id = %s", (CAMPAIGN_ID,))
        conn.execute(
            "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key) VALUES (%s, %s, 'Rook', 'rook')",
            (PARTICIPANT_ID, CAMPAIGN_ID),
        )
        conn.execute(
            "INSERT INTO campaign.documents (id, campaign_id, type, type_version, data, write_revision, "
            "field_revisions, name_key, search_key, linked_participant_id) VALUES (%s, %s, 'character-sheet', "
            "1, %s::jsonb, 1, %s::jsonb, 'rook', 'rook', %s)",
            (DOCUMENT_ID, CAMPAIGN_ID, '{"name": "Rook"}', '{"name": 1}', PARTICIPANT_ID),
        )
    assert mig.migrate(dsn, packaged=_THROUGH_ELIGIBILITY).applied == (ELIGIBILITY.filename,)
    mig.migrate(dsn)
    with connect(dsn) as conn:
        assert conn.execute(
            "SELECT authz_revision, projection_revision FROM campaign.authz_state WHERE campaign_id = %s",
            (CAMPAIGN_ID,),
        ).fetchone() == (3, 0)
        assert conn.execute(
            "SELECT alias, removed_at FROM campaign.participants WHERE id = %s", (PARTICIPANT_ID,)
        ).fetchone() == ("Rook", None)
        assert conn.execute(
            "SELECT linked_participant_id, write_revision FROM campaign.documents WHERE id = %s", (DOCUMENT_ID,)
        ).fetchone() == (PARTICIPANT_ID, 1)
        assert conn.execute("SELECT count(*) FROM campaign.projection_queue").fetchone() == (0,)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'New')", (OTHER_CAMPAIGN_ID, owner)
        )
        assert conn.execute(
            "SELECT authz_revision, projection_revision FROM campaign.authz_state WHERE campaign_id = %s",
            (OTHER_CAMPAIGN_ID,),
        ).fetchone() == (0, 0), "the trigger's row picks the new column's default up"
    assert mig.migrate(dsn).applied == ()
    with throwaway_database("fresh") as fresh:
        mig.migrate(fresh)
        assert _shape(dsn) == _shape(fresh)


def test_the_database_refuses_a_projection_revision_ahead_of_the_authorisation_revision(dsn):
    """T-A3: kills a dropped CHECK (M-A4)."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
        conn.execute(
            "UPDATE campaign.authz_state SET authz_revision = 2, projection_revision = 2 WHERE campaign_id = %s",
            (CAMPAIGN_ID,),
        )
        for projection in (3, -1):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "UPDATE campaign.authz_state SET projection_revision = %s WHERE campaign_id = %s",
                    (projection, CAMPAIGN_ID),
                )


_WILDCARDS = ["all", "*", "%", "motives.hidden_identity", "Name", "", "a" * 41, "1abc"]


@pytest.mark.parametrize("key", _WILDCARDS)
def test_the_database_refuses_a_wildcard_or_nested_field_key(dsn, key):
    """T-A4, for both tables that hold a key: kills a dropped `<> 'all'`
    (M-A5) and a regex widened to admit `.` or `*` (M-A6)."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_whole_campaign(conn, _one_user(conn))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.field_eligibility (document_id, field_key, campaign_id, eligibility_class, "
                "classification_source) VALUES (%s, %s, %s, 'public', 'gm')",
                (DOCUMENT_ID, key, CAMPAIGN_ID),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.projection_queue (campaign_id, document_id, field_key, authz_revision) "
                "VALUES (%s, %s, %s, 1)",
                (CAMPAIGN_ID, DOCUMENT_ID, key),
            )


def _classify(conn, eligibility_class: str, ids, source: str = "gm", key: str = "hp") -> None:
    conn.execute(
        "INSERT INTO campaign.field_eligibility (document_id, field_key, campaign_id, eligibility_class, "
        "principal_ids, classification_source) VALUES (%s, %s, %s, %s, %s, %s)",
        (DOCUMENT_ID, key, CAMPAIGN_ID, eligibility_class, ids, source),
    )


_P = ["prt_" + c * 22 for c in "abc"]


@pytest.mark.parametrize(
    ("eligibility_class", "ids"),
    [
        pytest.param("participants", None, id="null-list"),
        pytest.param("participants", [], id="empty-list"),
        pytest.param("participants", [None], id="null-element"),
        pytest.param("participants", ["grp_" + "a" * 22], id="wrong-prefix"),
        pytest.param("participants", [_P[0], _P[0]], id="duplicate"),
        pytest.param("participants", [_P[1], _P[0]], id="unsorted"),
        pytest.param("participants", [f"prt_{n:022d}" for n in range(101)], id="101-ids"),
        pytest.param("participants", [[_P[0]], [_P[1]]], id="two-dimensional"),
        pytest.param("characters", [_P[0]], id="participant-as-character"),
        pytest.param("gm_only", [_P[0]], id="list-on-gm-only"),
        pytest.param("campaign", [_P[0]], id="list-on-campaign"),
        pytest.param("public", [_P[0]], id="list-on-public"),
    ],
)
def test_the_database_refuses_a_malformed_principal_list(dsn, eligibility_class, ids):
    """T-A5: kills removing the COALESCE (M-A7), a lower bound of 0 (M-A8),
    `ELSE true` (M-A9) and a dropped ordering clause (M-A10)."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_whole_campaign(conn, _one_user(conn))
        with pytest.raises(psycopg.errors.CheckViolation):
            _classify(conn, eligibility_class, ids)


def test_a_well_formed_principal_list_and_each_listless_class_are_accepted(dsn):
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_whole_campaign(conn, _one_user(conn))
        _classify(conn, "participants", [f"prt_{n:022d}" for n in range(100)], key="ac")
        _classify(conn, "characters", [DOCUMENT_ID], key="hp")
        for key, eligibility_class in (("speed", "gm_only"), ("notes", "campaign"), ("name", "public")):
            _classify(conn, eligibility_class, None, key=key)
        assert conn.execute("SELECT count(*) FROM campaign.field_eligibility").fetchone() == (6,)


def test_only_a_gm_classification_is_wider_than_gm_only(dsn):
    """T-A6: kills a dropped `field_eligibility_only_gm_widens_chk` (M-A11)."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _a_whole_campaign(conn, _one_user(conn))
        with pytest.raises(psycopg.errors.CheckViolation):
            _classify(conn, "public", None, source="default")
        with pytest.raises(psycopg.errors.CheckViolation):
            _classify(conn, "gm_only", None, source="suggested")
        with pytest.raises(psycopg.errors.CheckViolation):
            _classify(conn, "unclassified", None)
        _classify(conn, "gm_only", None, source="default")


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        pytest.param(
            "INSERT INTO campaign.field_eligibility (document_id, field_key, campaign_id, eligibility_class, "
            "classification_source) VALUES (%s, 'hp', %s, 'public', 'gm')",
            (OTHER_DOCUMENT_ID, CAMPAIGN_ID),
            id="eligibility-row",
        ),
        pytest.param(
            "INSERT INTO campaign.projection_queue (campaign_id, document_id, field_key, authz_revision) "
            "VALUES (%s, %s, 'hp', 1)",
            (CAMPAIGN_ID, OTHER_DOCUMENT_ID),
            id="queue-item",
        ),
        pytest.param(
            "INSERT INTO campaign.group_members (group_id, participant_id, campaign_id) VALUES (%s, %s, %s)",
            (GROUP_ID, OTHER_PARTICIPANT_ID, CAMPAIGN_ID),
            id="membership-of-a-foreign-seat",
        ),
        pytest.param(
            "INSERT INTO campaign.group_members (group_id, participant_id, campaign_id) VALUES (%s, %s, %s)",
            (GROUP_ID, OTHER_PARTICIPANT_ID, OTHER_CAMPAIGN_ID),
            id="membership-of-a-foreign-group",
        ),
    ],
)
def test_rows_naming_another_campaigns_document_or_seat_are_refused(dsn, statement, params):
    """T-A7: kills a foreign key to `documents (id)` or `participants (id)`
    alone (M-A12)."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _two_campaigns(conn)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(statement, params)


def test_deleting_a_document_takes_its_eligibility_rows_and_queue_items(dsn):
    """T-A8's document half (the account half is the cascade test above, whose
    CAMPAIGN_TABLES now names 0019's four tables): kills a dropped
    `ON DELETE CASCADE` (M-A13)."""
    mig.migrate(dsn)
    with connect(dsn) as conn:
        _two_campaigns(conn)
        conn.execute("DELETE FROM campaign.documents WHERE id = %s", (DOCUMENT_ID,))
        assert conn.execute("SELECT count(*) FROM campaign.field_eligibility").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM campaign.projection_queue").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM campaign.group_members").fetchone() == (1,)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("id", "grp_short", id="too-few-bits"),
        pytest.param("id", "group_" + "a" * 22, id="no-prefix"),
        pytest.param("name", "", id="empty-name"),
        pytest.param("name", "n" * 41, id="41-character-name"),
        pytest.param("name_fold", "", id="empty-key"),
        pytest.param("name_fold", "k" * 201, id="201-character-key"),
        pytest.param("created_command_id", "short", id="malformed-command-id"),
    ],
)
def test_the_database_refuses_a_group_row_the_application_would_never_mint(dsn, column, value):
    """T-A9: kills a loosened group CHECK (M-A14)."""
    mig.migrate(dsn)
    row = {"id": GROUP_ID, "name": "Scouts", "name_fold": "scouts", "created_command_id": None} | {column: value}
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO campaign.groups (id, campaign_id, name, name_fold, created_command_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (row["id"], CAMPAIGN_ID, row["name"], row["name_fold"], row["created_command_id"]),
            )


def test_the_documents_composite_key_leaves_every_document_write_a_no_key_update(dsn):
    """T-A10, as critic item 10 rewrote it. Connection A holds an uncommitted
    eligibility row AND an uncommitted queue item for the sheet: each takes
    FOR KEY SHARE on the document row through its foreign key. Connection B
    then runs every `PostgresDocumentStore` method that UPDATEs
    `campaign.documents`, each under a 200 ms lock timeout. If the key those
    foreign keys target (0018's `documents_id_campaign_key`) made an updated
    column a key column (M-A15: `UNIQUE (id, campaign_id, updated_at)`), that
    UPDATE would need FOR UPDATE and wait on A."""
    from datetime import UTC, datetime

    from service.db import Database, PoolSettings
    from service.document_store import PostgresDocumentStore
    from service.workbench_contracts import Author, DocumentTypeId

    mig.migrate(dsn)
    store = PostgresDocumentStore()
    database = Database(dsn, PoolSettings(sync_max=2, async_max=0, acquire_timeout_s=5))
    with connect(dsn) as conn:
        owner = _one_user(conn)
        conn.execute(
            "INSERT INTO campaign.campaigns (id, owner_id, name) VALUES (%s, %s, 'Nocturne')", (CAMPAIGN_ID, owner)
        )
        conn.execute(
            "INSERT INTO campaign.participants (id, campaign_id, alias, alias_key) VALUES (%s, %s, 'Rook', 'rook')",
            (PARTICIPANT_ID, CAMPAIGN_ID),
        )
    with database.transaction() as unit:
        sheet = store.create(
            unit,
            CAMPAIGN_ID,
            doc_type=DocumentTypeId.CHARACTER_SHEET,
            type_version=1,
            data={"name": "Rook", "qualifier": "", "tags": []},
            author=Author.GM,
        ).id
    later = datetime(2030, 1, 1, tzinfo=UTC)
    writes = [
        lambda unit: store.write_fields(
            unit, CAMPAIGN_ID, sheet, fields={"qualifier": "Rogue"}, author=Author.GM,
            base_write_revision=None, now=later,
        ),
        lambda unit: store.restore(unit, CAMPAIGN_ID, sheet, version_number=1, now=later),
        lambda unit: store.set_archived(unit, CAMPAIGN_ID, sheet, archived=True, now=later),
        lambda unit: store.link_character_sheet(unit, CAMPAIGN_ID, sheet, participant_id=PARTICIPANT_ID),
        lambda unit: store.unlink_character_sheet(unit, CAMPAIGN_ID, sheet),
    ]
    with connect(dsn, autocommit=False) as holder:
        holder.execute(
            "INSERT INTO campaign.field_eligibility (document_id, field_key, campaign_id, eligibility_class, "
            "classification_source) VALUES (%s, 'hp', %s, 'public', 'gm')",
            (sheet, CAMPAIGN_ID),
        )
        holder.execute(
            "INSERT INTO campaign.projection_queue (campaign_id, document_id, field_key, authz_revision) "
            "VALUES (%s, %s, 'hp', 1)",
            (CAMPAIGN_ID, sheet),
        )
        for write in writes:
            with database.transaction() as unit:
                unit.conn.execute("SET LOCAL lock_timeout = '200ms'")
                write(unit)
        holder.rollback()
