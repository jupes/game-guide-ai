"""What the campaign SQL and the campaign Python promise each other (1kg.2.1).

Two agreements are checked here, and both are the kind that rot silently.

**The identifier rule is written twice** — once as a POSIX regular expression in
`0004_campaign_schema.sql`'s CHECK constraints, once as
`service/campaign_identity.id_check_regex`. One rule spelled twice drifts, so
this file reads the migration and requires the two to be the same text.

**Digests only, at rest and in output** (SEC-5). No column of 0004 or 0005 holds
a token, a code or a credential, every digest column carries the same 64-hex
CHECK, and the only places in the store modules where a minted secret exists at
all are the four methods that hand it to their caller and immediately forget it.

These are text assertions on files, so they need no database and run anywhere.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import get_args

import pytest

from service import (
    audit_log,
    campaign_store,
    campaigns_api,
    db,
    eligibility_store,
    participant_store,
    reconciliation,
    reveal_store,
    seat_offer_store,
    table_session_store,
    table_sessions,
    workbench_contracts,
)
from service import campaign_identity as ident
from service.workbench_contracts import (
    ALT_MAX_CHARS,
    AMBIENCE_MAX_MS,
    ASSET_MAX_BYTES,
    IMAGE_MAX_PIXELS,
    IMAGE_MAX_SIDE,
    MEDIA_TYPES,
    AssetFailure,
    AssetKind,
    AssetState,
    CommandId,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "sql" / "migrations"
CAMPAIGN_SQL = (MIGRATIONS / "0004_campaign_schema.sql").read_text(encoding="utf-8")
AUDIT_SQL = (MIGRATIONS / "0005_audit_events.sql").read_text(encoding="utf-8")
CONVERSATION_SQL = (MIGRATIONS / "0006_conversation_metadata.sql").read_text(encoding="utf-8")
DOCUMENT_SQL = (MIGRATIONS / "0008_document_schema.sql").read_text(encoding="utf-8")
SEAT_SQL = (MIGRATIONS / "0009_participant_accounts.sql").read_text(encoding="utf-8")
SESSION_ACCESS_SQL = (MIGRATIONS / "0016_table_session_access.sql").read_text(encoding="utf-8")
#: Found by its name, never by its number: the lead renumbers a migration at
#: merge when a parallel bead takes the number first (R-7).
[MEDIA_SQL_PATH] = sorted(MIGRATIONS.glob("*_media_assets.sql"))
MEDIA_SQL = MEDIA_SQL_PATH.read_text(encoding="utf-8")
#: `1kg.7.1`'s disclosures and slots, found by name for the same reason.
[REVEAL_SQL_PATH] = sorted(MIGRATIONS.glob("*_reveal_disclosures.sql"))
REVEAL_SQL = REVEAL_SQL_PATH.read_text(encoding="utf-8")

#: Every migration, sorted and concatenated. The two identifier tests below read
#: THIS rather than one file: the prefix registry is service-wide, so a prefix
#: constrained in any migration satisfies it and a prefix nobody mints is a
#: leftover wherever it sits. Reading one file made adding a table in a later
#: migration break a test that had nothing to say about it.
ALL_SQL = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql"))
)

STORE_MODULES = (
    campaign_store,
    participant_store,
    table_session_store,
    table_sessions,
    audit_log,
    seat_offer_store,
    reconciliation,
    reveal_store,
    eligibility_store,
)


# ── The identifier rule, spelled once ────────────────────────────────────────


def test_every_minted_prefix_is_constrained_in_the_migration_by_the_registrys_own_regex():
    for prefix in ident.PREFIXES:
        expected = ident.id_check_regex(prefix)
        assert f"CHECK (id ~ '{expected}')" in ALL_SQL, (
            f"no migration constrains {prefix!r} with {expected!r}"
        )


def test_the_migration_constrains_no_identifier_the_registry_has_never_heard_of():
    """The other direction: a CHECK for a prefix nobody mints is a column the
    application can never fill, and a prefix retired from the registry would
    leave one behind."""
    in_sql = set(re.findall(r"CHECK \(id ~ '\^([a-z]{3}_)", ALL_SQL))
    assert in_sql == set(ident.PREFIXES)


def test_a_minted_identifier_satisfies_the_constraint_the_migration_carries():
    """The regexes agreeing as text is not quite the point; this is."""
    for prefix in ident.PREFIXES:
        pattern = re.compile(ident.id_check_regex(prefix))
        assert pattern.fullmatch(ident.new_id(prefix)) is not None
        assert pattern.fullmatch(prefix + "short") is None


# ── Digests only, at rest ────────────────────────────────────────────────────

#: A column whose name contains one of these would be holding the thing itself.
RAW_WORDS = ("token", "code", "secret", "credential", "password", "passphrase")
#: ... unless it is one of these. The digests are the point of SEC-5;
#: `reason_code` is a closed vocabulary, not a secret; and `lock_token` holds no
#: value at all — it is never written, and exists only so that 1ir.1.13 can
#: grant a display role UPDATE on one column of `authz_state` (RQ-1).
ALLOWED = (
    "code_digest", "credential_digest", "link_digest", "reason_code", "lock_token",
    "enrolment_codes", "table_credentials", "device_credentials",
)

DIGEST_CHECK = r"~ '\^\[0-9a-f\]\{64\}\$'"


def _columns(sql: str) -> list[tuple[str, str]]:
    """Every `(table, column)` a CREATE TABLE in `sql` declares, with the line.

    CREATE TABLE only, which is why `MIGRATION_FILES` below holds 0004 and 0005
    and not 0006: 0006 is an ALTER, and its four columns are pinned by name in
    `test_the_conversation_columns_are_the_four_agreed_and_carry_no_cascade`.
    """
    found: list[tuple[str, str]] = []
    table = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("CREATE TABLE"):
            table = stripped.split()[2]
            continue
        if stripped.startswith(")"):
            table = ""
            continue
        if not table or stripped.startswith(("--", "PRIMARY KEY")):
            continue
        match = re.match(r"^([a-z_]+)\s+[A-Z]", stripped)
        if match:
            found.append((table, stripped))
    return found


MIGRATION_FILES = [
    pytest.param("0004", CAMPAIGN_SQL, id="0004"),
    pytest.param("0005", AUDIT_SQL, id="0005"),
    pytest.param("0007", DOCUMENT_SQL, id="0007"),
    pytest.param("media_assets", MEDIA_SQL, id="media_assets"),
    pytest.param("reveal_disclosures", REVEAL_SQL, id="reveal_disclosures"),
]

#: Files that deliberately store no digest at all, each for its own reason, and
#: for which the digest test below asserts the ABSENCE rather than dropping the
#: guard: 0005 because an audit row carries ids and codes and no hash of
#: anything (ED-26), 0007 because a document holds content and not secrets.
NO_DIGEST_FILES = {
    "0005": "the audit ledger stores no digest of anything (ED-26)",
    "0007": "a document holds field content, never a secret (SEC-20)",
    "media_assets": "an asset row holds object keys, never a secret",
    "reveal_disclosures": "a disclosure holds ids, a version, field keys and codes, never a secret",
}


@pytest.mark.parametrize(("name", "sql"), MIGRATION_FILES)
def test_no_column_of_the_campaign_schema_is_named_as_though_it_held_a_raw_secret(
    name: str, sql: str
):
    """A name-based check, and it claims no more than that. It catches the
    mistake that actually happens — a column called `link_token` beside the
    digest — and cannot catch a raw secret hidden behind an innocuous name.
    What rules that out is `test_a_minted_secret_is_returned_and_never_written_anywhere`
    below, which reads the statements rather than the column names."""
    for table, line in _columns(sql):
        column = line.split()[0]
        if column in ALLOWED:
            continue
        offending = [word for word in RAW_WORDS if word in column]
        assert not offending, f"{name}: {table}.{column} reads like it holds a {offending[0]}"


@pytest.mark.parametrize(("name", "sql"), MIGRATION_FILES)
def test_every_digest_column_is_checked_to_be_a_sha256_hex_digest(name: str, sql: str):
    digests = [(table, line) for table, line in _columns(sql) if line.split()[0].endswith("_digest")]
    if name in NO_DIGEST_FILES:
        assert digests == [], NO_DIGEST_FILES[name]
        return
    assert digests, f"{name}: this test found no digest column, so it proves nothing"
    for table, line in digests:
        assert re.search(DIGEST_CHECK, line), (
            f"{name}: {table}.{line.split()[0]} is not checked to be 64 hex characters"
        )


def test_the_only_digest_columns_are_the_four_the_threat_model_names():
    digests = {
        f"{table}.{line.split()[0]}" for table, line in _columns(CAMPAIGN_SQL)
        if line.split()[0].endswith("_digest")
    }
    assert digests == {
        "campaign.enrolment_codes.code_digest",
        "campaign.device_credentials.credential_digest",
        "campaign.table_sessions.link_digest",
        "campaign.table_credentials.credential_digest",
    }


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("campaign.enrolment_codes", "code_digest"),
        ("campaign.device_credentials", "credential_digest"),
        ("campaign.table_sessions", "link_digest"),
        ("campaign.table_credentials", "credential_digest"),
    ],
)
def test_every_digest_is_looked_up_by_a_unique_index(table: str, column: str):
    """"Digest lookups are exact matches on a unique index" — the alignment's
    words, and the migration header's, and ARCHITECTURE.md's. `link_digest` is
    the one every unauthenticated join is found by and it had no index at all,
    so the sentence was three-quarters true; this is what makes it four."""
    index = re.search(
        rf"CREATE UNIQUE INDEX (\w+)\s+ON {re.escape(table)} \({column}\)(.*)", CAMPAIGN_SQL
    )
    assert index is not None, f"{table}.{column} has no unique index"
    if column == "link_digest":
        # It must stay PARTIAL: PostgreSQL counts the columns of a non-partial
        # unique index as key columns, so a full one here would make Rotate's
        # UPDATE take FOR UPDATE and start conflicting with the FOR KEY SHARE a
        # join's foreign-key check takes (RQ-3). Proved on a server in
        # `tests/test_campaign_db.py`; pinned as text here.
        assert "WHERE link_digest IS NOT NULL" in index.group(2)


def test_a_sessions_gm_is_the_campaigns_owner_by_foreign_key():
    """AUD-1 held by the database rather than by a route remembering to compare
    two values, which is what `docs/migrations.md` section 4 asks for."""
    assert re.search(
        r"FOREIGN KEY \(campaign_id, gm_user_id\)\s*\n?\s*"
        r"REFERENCES campaign\.campaigns \(id, owner_id\)",
        CAMPAIGN_SQL,
    ), "table_sessions does not tie its GM to the campaign's owner"
    assert "UNIQUE (id, owner_id)" in CAMPAIGN_SQL, "the key that edge references"


@pytest.mark.parametrize(
    "invariant",
    [
        "CHECK ((state = 'live') = (ended_at IS NULL))",
        "CHECK (expires_at > started_at)",
        "CHECK (consumed_at IS NULL OR revoked_at IS NULL)",
    ],
)
def test_the_states_the_schema_will_not_hold(invariant: str):
    """Each of these was a pair of columns that could contradict each other: a
    live session with an ending, a session that expired before it began, and a
    code that was both spent and cancelled — which would make "was this link
    ever used?" unanswerable."""
    assert invariant in CAMPAIGN_SQL


def test_the_alias_index_compares_a_key_the_application_computes():
    """`lower(alias)` answered according to the database's collation provider
    while the twin answered with Python, and the two disagree about a final
    sigma and a dotted capital I — silently. The key is computed in one place
    now (`service/participant_store.alias_key`), so the two worlds agree by
    construction rather than by luck."""
    assert "ON campaign.participants (campaign_id, alias_key) WHERE removed_at IS NULL" in CAMPAIGN_SQL
    statements = "\n".join(
        line for line in CAMPAIGN_SQL.splitlines() if not line.lstrip().startswith("--")
    )
    assert "lower(alias)" not in statements, "the index no longer asks the database to fold"


def test_the_alias_key_bound_is_the_number_the_migration_checks():
    """G-1. NFKC expands — one `U+FDFA` folds to eighteen characters — so the
    alias's own bound of 40 does not bound the key. `check_alias` refuses a key
    over `ALIAS_KEY_MAX`; if that number and the column's CHECK ever disagreed,
    the twin would seat a row PostgreSQL refuses with an error quoting the
    alias, which is the one thing SEC-20 forbids."""
    found = re.search(
        r"alias_key\s+TEXT NOT NULL CHECK \(length\(alias_key\) BETWEEN 1 AND (\d+)\)", CAMPAIGN_SQL
    )
    assert found is not None, "0004 no longer bounds alias_key"
    assert participant_store.ALIAS_KEY_MAX == int(found.group(1))


def test_the_conversation_columns_are_the_four_agreed_and_carry_no_cascade():
    """Requirement 5, and the fail-closed half of it: until 1kg.2.6 decides the
    deletion order, deleting a campaign that still has conversations is refused
    rather than silently detaching or destroying them."""
    added = set(re.findall(r"ADD COLUMN (\w+)", CONVERSATION_SQL))
    assert added == {"campaign_id", "title", "updated_at", "archived_at"}
    assert "mode" not in added, "a mode is per turn, not per conversation"

    reference = re.search(
        r"campaign_id\s+TEXT REFERENCES campaign\.campaigns \(id\)(.*?),\n", CONVERSATION_SQL, re.S
    )
    assert reference is not None
    assert "ON DELETE" not in reference.group(1), "the campaign reference takes no delete action"
    assert "DEFERRABLE INITIALLY DEFERRED" in reference.group(1), (
        "checked at commit, so deleting an account no longer depends on trigger OID order"
    )


# ── 0009: a participant is an account's seat (agent-forge-harness-fma) ──────


def _statements(sql: str) -> str:
    """The file without its comment lines, so a pin on a clause cannot be
    satisfied by the prose that explains it."""
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_the_seat_migration_adds_the_account_with_no_delete_action_and_one_check():
    """L-1 and L-2. `NO ACTION` is written out: CASCADE would delete a seat the
    disclosures, the character-sheet link and the audit rows reference, and
    SET NULL would re-open an accepted seat to whoever next accepted it. The one
    CHECK is safe only because both columns it reads are new here; a second
    CHECK added by ALTER TABLE would validate every existing row."""
    body = _statements(SEAT_SQL)
    assert set(re.findall(r"ADD COLUMN (\w+)", body)) == {"user_id", "accepted_at"}
    assert "ADD COLUMN user_id BIGINT REFERENCES auth.users (id) ON DELETE NO ACTION," in body
    assert "ADD COLUMN accepted_at TIMESTAMPTZ," in body
    assert "CHECK (accepted_at IS NULL OR user_id IS NOT NULL)" in body
    assert body.count("CHECK") == 1, "the only CHECK this file may add"
    assert "CASCADE" not in body and "SET NULL" not in body
    assert "NOT NULL" not in body.replace("IS NOT NULL", ""), "both columns stay nullable"


def test_the_seat_migration_carries_both_partial_indexes_and_both_drops():
    """L-3 and L-8: one live seat per account per campaign, the account's own
    list, and the two tables D-1 and D-4 retired. No transaction control: the
    runner owns the transaction (docs/migrations.md section 2)."""
    body = _statements(SEAT_SQL)
    assert re.search(
        r"CREATE UNIQUE INDEX \w+\s+ON campaign\.participants \(campaign_id, user_id\)\s+"
        r"WHERE removed_at IS NULL AND user_id IS NOT NULL;",
        body,
    ), "one live seat per account per campaign"
    assert re.search(
        r"CREATE INDEX \w+\s+ON campaign\.participants \(user_id\) WHERE removed_at IS NULL;",
        body,
    ), "the account's own list of its live seats"
    assert "CONCURRENTLY" not in body
    assert re.findall(r"DROP TABLE ([\w.]+);", body) == [
        "campaign.enrolment_codes",
        "campaign.device_credentials",
    ]
    assert not re.search(r"\b(BEGIN|COMMIT|END|ROLLBACK)\s*;", body)


def test_the_seat_migration_states_why_its_check_and_its_drops_are_safe():
    """The two proofs L-1 and L-8 ask the file itself to carry, so that the
    reason for a contraction is read where the contraction is."""
    prose = " ".join(
        line.lstrip("- ").strip() for line in SEAT_SQL.splitlines() if line.startswith("--")
    )
    assert "Both columns it reads are new in this file" in prose
    assert "the previous build writes neither column" in prose
    assert (
        "No build that reads campaign.enrolment_codes or campaign.device_credentials "
        "has ever been deployed"
    ) in prose



# ── 0016: the table session without a link or a join (1kg.2.3) ──────────────

#: The wire contract's `CommandId` pattern, read off the contract itself.
COMMAND_ID_PATTERN = get_args(workbench_contracts.CommandId)[1].pattern


def test_the_session_access_migration_is_exactly_its_five_changes():
    """L-3, pinned as text: both drops, both columns with the contract's own
    `CommandId` pattern, the start index's predicate, no index at all on the
    rotate column, and the actor kinds. No transaction control, not
    CONCURRENTLY: the runner owns the transaction."""
    body = _statements(SESSION_ACCESS_SQL)
    assert re.findall(r"ALTER TABLE campaign\.table_sessions DROP COLUMN (\w+);", body) == [
        "link_digest"
    ]
    assert re.findall(r"DROP TABLE ([\w.]+);", body) == ["campaign.session_join_counters"]
    for column in ("start_command_id", "rotate_command_id"):
        check = re.search(
            rf"ADD COLUMN {column} TEXT\s+CHECK \({column} IS NULL OR {column} ~ '([^']+)'\)", body
        )
        assert check is not None, column
        assert check.group(1) == COMMAND_ID_PATTERN == table_session_store.COMMAND_ID.pattern
    assert re.search(
        r"CREATE UNIQUE INDEX \w+\s+ON campaign\.table_sessions \(campaign_id, start_command_id\)\s+"
        r"WHERE start_command_id IS NOT NULL;",
        body,
    ), "one session per start command per campaign, partial so that it is not a key"
    indexes = re.findall(r"CREATE (?:UNIQUE )?INDEX[^;]*;", body)
    assert len(indexes) == 1 and not [i for i in indexes if "rotate_command_id" in i]
    actor = re.search(
        r"ADD CONSTRAINT events_actor_kind_check\s+CHECK \(actor_kind IN \(([^)]*)\)\);", body
    )
    assert actor is not None
    assert {v.strip().strip("'") for v in actor.group(1).split(",")} == {
        "gm", "participant", "screen", "system"
    }
    assert "ALTER TABLE audit.events DROP CONSTRAINT events_actor_kind_check;" in body
    assert "CONCURRENTLY" not in body
    assert not re.search(r"(BEGIN|COMMIT|END|ROLLBACK)\s*;", body)


def test_the_session_access_migration_states_its_three_proofs():
    """The file itself says why each change is safe, where the change is."""
    prose = " ".join(
        line.lstrip("- ").strip() for line in SESSION_ACCESS_SQL.splitlines() if line.startswith("--")
    )
    assert (
        "no build that reads link_digest or session_join_counters has ever been deployed"
    ) in prose
    assert "the store that ships in the same change as this file reads neither" in prose
    assert "The new columns' CHECKs validate only NULLs" in prose
    assert "the previous build writes neither column" in prose
    assert "no code has ever written 'guest' to audit.events" in prose

# ── Digests only, in output ──────────────────────────────────────────────────

#: The one method allowed to hand a caller a secret in plain text — the moment
#: it is minted, and the only moment it exists — beside a record whose stored
#: form is the digest. The table link and the join credential are gone
#: (threat model section 15, `1kg.2.3`), so Start and Rotate mint nothing; the
#: screen grant is the one bearer secret left (SEC-48). The lifecycle service's
#: `mint_screen` carries it on in a record and is covered by the scan above.
MINTING_METHODS = {
    "mint_screen",
}


@pytest.mark.parametrize(
    "module", STORE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_a_minted_secret_is_returned_and_never_written_anywhere(module):
    """The grep the acceptance criteria ask for, made precise. A minted secret
    exists in these modules under exactly one name, and every line that mentions
    it is the line that hands it back — so none of them can be a parameter of a
    statement, a log call or a field of a record."""
    source = inspect.getsource(module)
    mentions = [line.strip() for line in source.splitlines() if ".secret" in line]
    for line in mentions:
        assert line.startswith("return "), f"a minted secret is used other than to return it: {line}"


@pytest.mark.parametrize(
    "module", STORE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1]
)
def test_only_the_minting_methods_hand_back_a_plain_text_secret(module):
    """A reviewer should be able to name every method that produces one. If a
    later method starts returning a bare string beside a record, this fails and
    the question gets asked rather than assumed."""
    for _, store in inspect.getmembers(module, inspect.isclass):
        if store.__module__ != module.__name__:
            continue
        for name, method in inspect.getmembers(store, inspect.isfunction):
            if name.startswith("_"):
                continue
            returns = str(inspect.signature(method).return_annotation)
            if "str]" in returns and returns.startswith("tuple["):
                assert name in MINTING_METHODS, f"{store.__name__}.{name} returns {returns}"


# ── The seat confirmation and offers (bead 1kg.2.2) ──────────────────────────

#: The one place this test file names the migration, so renumbering it at merge
#: is one edit here.
OFFER_MIGRATION = "0012_seat_confirmation_and_offers.sql"
OFFER_SQL = (MIGRATIONS / OFFER_MIGRATION).read_text(encoding="utf-8")


def test_the_offer_migration_adds_the_confirmation_its_one_check_and_the_unique_key():
    body = _statements(OFFER_SQL)
    assert "ALTER TABLE campaign.participants\n  ADD COLUMN confirmed_at TIMESTAMPTZ," in body
    assert (
        "ADD CONSTRAINT participants_confirmed_is_accepted_chk\n"
        "    CHECK (confirmed_at IS NULL OR accepted_at IS NOT NULL)," in body
    )
    assert "ADD CONSTRAINT participants_id_campaign_key UNIQUE (id, campaign_id);" in body
    assert re.findall(r"ADD COLUMN (\w+)", body) == ["confirmed_at"], "the only column added to an old table"
    assert body.count("ADD CONSTRAINT") == 2


def test_the_offer_table_carries_both_composite_keys_and_every_check():
    body = _statements(OFFER_SQL)
    assert (
        "FOREIGN KEY (participant_id, campaign_id)\n"
        "    REFERENCES campaign.participants (id, campaign_id) ON DELETE CASCADE," in body
    )
    assert (
        "FOREIGN KEY (campaign_id, offered_by)\n"
        "    REFERENCES campaign.campaigns (id, owner_id) ON DELETE CASCADE" in body
    )
    for check in (
        "address        TEXT NOT NULL CHECK (length(address) BETWEEN 3 AND 254),",
        "address_key    TEXT NOT NULL CHECK (length(address_key) BETWEEN 3 AND 254 AND address_key !~ '[A-Z]'),",
        "outcome        TEXT CHECK (outcome IN ('accepted', 'declined', 'expired', 'withdrawn')),",
        "CHECK (expires_at > created_at),",
        "CHECK ((outcome IS NULL) = (answered_at IS NULL)),",
    ):
        assert check in body, check


def test_the_offer_indexes_carry_their_predicates_and_none_reads_the_clock():
    body = _statements(OFFER_SQL)
    indexes = re.findall(r"CREATE (UNIQUE )?INDEX (\w+)\s+ON campaign\.seat_offers ([^;]+);", body)
    assert [(unique.strip(), name, on.strip()) for unique, name, on in indexes] == [
        ("UNIQUE", "seat_offers_one_open_per_seat_uidx", "(participant_id) WHERE outcome IS NULL"),
        ("", "seat_offers_seat_latest_idx", "(participant_id, created_at)"),
        ("", "seat_offers_open_by_address_idx", "(address_key) WHERE outcome IS NULL"),
        ("", "seat_offers_campaign_address_idx", "(campaign_id, address_key)"),
        ("", "seat_offers_throttle_idx", "(offered_by, created_at)"),
    ]
    assert "now()" not in " ".join(on for _, _, on in indexes)
    assert "UNIQUE INDEX seat_offers_campaign_address" not in body, "deliberately no unique (campaign, key)"
    assert "CONCURRENTLY" not in body
    assert not re.search(r"\b(BEGIN|COMMIT|END|ROLLBACK)\s*;", body)


def test_the_block_table_is_keyed_by_both_accounts_and_cascades_from_each():
    body = _statements(OFFER_SQL)
    assert "blocker_user_id  BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE," in body
    assert "blocked_owner_id BIGINT NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE," in body
    assert "PRIMARY KEY (blocker_user_id, blocked_owner_id)," in body
    assert "CHECK (blocker_user_id <> blocked_owner_id)" in body


def test_the_offer_id_is_constrained_by_the_registrys_own_regex():
    assert f"CHECK (id ~ '{ident.id_check_regex(ident.SEAT_OFFER)}')" in OFFER_SQL


def test_the_offer_migration_states_why_its_check_and_its_key_are_safe():
    prose = " ".join(line.lstrip("- ").strip() for line in OFFER_SQL.splitlines() if line.startswith("--"))
    assert "The column it constrains is new in this file" in prose
    assert "the previous build never writes confirmed_at" in prose
    assert "Nothing updates a participant's id or campaign_id" in prose


#: Everything the offer route runs, from ownership to the insert (L-6). None of
#: it may name an account or a block: the answer must not depend on whether an
#: account holds the address (SEC-50(2)).
OFFER_PATH = (
    seat_offer_store.PostgresSeatOfferStore.create,
    seat_offer_store.PostgresSeatOfferStore.is_repeat,
    seat_offer_store.PostgresSeatOfferStore.open_for_seat,
    seat_offer_store.PostgresSeatOfferStore.expire_stale,
    seat_offer_store.PostgresSeatOfferStore.count_recent,
    participant_store.PostgresParticipantStore.hold,
    campaign_store.PostgresCampaignStore.get,
    campaigns_api.offer_seat,
)


@pytest.mark.parametrize("step", OFFER_PATH, ids=lambda f: f.__qualname__)
def test_the_offer_path_names_no_account_and_no_block(step):
    source = inspect.getsource(step)
    for table in ("auth.users", "seat_blocks", "is_blocked", "get_credentials", "get_user"):
        assert table not in source, f"{step.__qualname__} reads {table}"


# ── Media assets (agent-forge-harness-1kg.8.1.1, L-2 to L-4) ─────────────────
#
# One rule spelled twice drifts, so every set and every bound the migration
# writes out is held here to the wire contract's constant it mirrors.


def _table(sql: str, name: str) -> str:
    """The body of `CREATE TABLE name (...)`, comments dropped."""
    body = _statements(sql)
    found = re.search(rf"CREATE TABLE {re.escape(name)} \((.*?)\n\);", body, re.S)
    assert found is not None, f"no CREATE TABLE {name}"
    return found.group(1)


def _quoted(listing: str) -> set[str]:
    return set(re.findall(r"'([^']*)'", listing))


ASSETS_SQL = _table(MEDIA_SQL, "campaign.assets")
USAGE_SQL = _table(MEDIA_SQL, "campaign.media_usage")


def test_the_media_tables_are_found_by_the_column_parser():
    """The raw-secret test above reads columns one per line; a layout it cannot
    parse would make it check nothing at all."""
    columns = {(table, line.split()[0]) for table, line in _columns(MEDIA_SQL)}
    assert columns, "no column was found, so the raw-secret test checked nothing"
    assert {column for table, column in columns if table == "campaign.assets"} == {
        "id", "campaign_id", "kind", "state", "failure", "declared_media_type", "declared_size_bytes",
        "media_type", "size_bytes", "width", "height", "duration_ms", "alt", "object_key", "tmp_key",
        "created_command_id", "created_at", "updated_at", "state_changed_at",
    }
    assert {column for table, column in columns if table == "campaign.media_usage"} == {
        "campaign_id", "bytes_reserved", "asset_count",
    }


def test_the_asset_id_is_checked_by_the_registrys_own_regex():
    assert f"CHECK (id ~ '{ident.id_check_regex(ident.ASSET)}')" in ASSETS_SQL


def test_an_asset_names_its_campaign_with_no_delete_action_and_nothing_cascades_to_it():
    """L-3(b): NO ACTION, written out. A cascade would remove the rows and leave
    their bytes in the bucket with nothing naming them (SEC-29, SEC-36)."""
    assert re.search(
        r"^\s*campaign_id\s+TEXT NOT NULL REFERENCES campaign\.campaigns \(id\) ON DELETE NO ACTION,$",
        ASSETS_SQL,
        re.M,
    )
    assert "CASCADE" not in ASSETS_SQL and "SET NULL" not in ASSETS_SQL
    assert re.search(
        r"^\s*campaign_id\s+TEXT PRIMARY KEY REFERENCES campaign\.campaigns \(id\) ON DELETE CASCADE,$",
        USAGE_SQL,
        re.M,
    ), "the usage row holds counts, never content, and goes with its campaign"


def test_the_closed_sets_are_the_enums_they_mirror():
    kinds = re.search(r"kind\s+TEXT NOT NULL CHECK \(kind IN \(([^)]*)\)\)", ASSETS_SQL)
    states = re.search(r"state\s+TEXT NOT NULL CHECK \(state IN \(([^)]*)\)\)", ASSETS_SQL)
    failures = re.search(r"failure\s+TEXT CHECK \(failure IN \(([^)]*)\)\)", ASSETS_SQL)
    assert kinds and states and failures
    assert _quoted(kinds.group(1)) == {kind.value for kind in AssetKind}
    assert _quoted(states.group(1)) == {state.value for state in AssetState} | {"deleted"}, (
        "deleted is a storage-only tombstone and never joins the contract"
    )
    assert "deleted" not in {state.value for state in AssetState}
    assert _quoted(failures.group(1)) == {failure.value for failure in AssetFailure}
    declared = dict(re.findall(r"kind = '(\w+)' AND declared_media_type IN \(([^)]*)\)", ASSETS_SQL))
    assert {AssetKind(kind): _quoted(types) for kind, types in declared.items()} == {
        kind: set(types) for kind, types in MEDIA_TYPES.items()
    }


def test_every_bound_is_the_wire_contracts_number():
    caps = re.findall(r"CASE kind WHEN 'image' THEN (\d+) WHEN 'audio' THEN (\d+) END", ASSETS_SQL)
    assert caps == [(str(ASSET_MAX_BYTES[AssetKind.IMAGE]), str(ASSET_MAX_BYTES[AssetKind.AUDIO]))] * 2, (
        "the declared size and the ready size, each under its kind's cap"
    )
    for side in ("width", "height"):
        assert f"CHECK ({side} BETWEEN 1 AND {IMAGE_MAX_SIDE})" in ASSETS_SQL
    assert f"CHECK (width::bigint * height <= {IMAGE_MAX_PIXELS})" in ASSETS_SQL
    assert f"CHECK (duration_ms BETWEEN 1 AND {AMBIENCE_MAX_MS})" in ASSETS_SQL
    assert f"CHECK (length(alt) BETWEEN 1 AND {ALT_MAX_CHARS})" in ASSETS_SQL
    command = next(m.pattern for m in CommandId.__metadata__ if getattr(m, "pattern", None))
    assert f"CHECK (created_command_id ~ '{command}')" in ASSETS_SQL


def test_what_was_measured_exists_exactly_on_a_ready_row():
    """L-3(e), which is the wire `Asset` validator's own rule."""
    measured = re.search(r"CONSTRAINT assets_measured_chk CHECK \((.*?)\),\n", ASSETS_SQL, re.S)
    assert measured is not None
    clauses = {" ".join(clause.split()) for clause in re.split(r"\s+AND (?=\()", measured.group(1))}
    assert clauses == {
        "(media_type IS NOT NULL) = (state = 'ready')",
        "(size_bytes IS NOT NULL) = (state = 'ready')",
        "(width IS NOT NULL) = (kind = 'image' AND state = 'ready')",
        "(height IS NOT NULL) = (kind = 'image' AND state = 'ready')",
        "(duration_ms IS NOT NULL) = (kind = 'audio' AND state = 'ready')",
    }
    assert "CHECK ((failure IS NOT NULL) = (state = 'failed'))" in ASSETS_SQL
    assert "CASE WHEN state = 'deleted' THEN alt IS NULL ELSE (alt IS NOT NULL) = (kind = 'image') END" in ASSETS_SQL
    assert "(kind = 'image' AND media_type = declared_media_type)" in ASSETS_SQL
    assert "(kind = 'audio' AND media_type = 'audio/mpeg')" in ASSETS_SQL


def test_both_keys_are_checked_and_unique():
    assert "object_key          TEXT NOT NULL UNIQUE CHECK (object_key ~ '^assets/[0-9a-f]{32}$')," in ASSETS_SQL
    assert "tmp_key             TEXT NOT NULL UNIQUE CHECK (tmp_key ~ '^tmp/[0-9a-f]{32}$')," in ASSETS_SQL


def test_the_command_index_is_partial_and_the_one_other_index_is_campaign_and_state():
    body = _statements(MEDIA_SQL)
    assert re.search(
        r"CREATE UNIQUE INDEX \w+\s+ON campaign\.assets \(campaign_id, created_command_id\) "
        r"WHERE created_command_id IS NOT NULL;",
        body,
    )
    indexes = re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)\s+ON campaign\.assets \(([^)]*)\)", body)
    assert sorted(columns for _, columns in indexes) == ["campaign_id, created_command_id", "campaign_id, state"]


def test_the_usage_row_is_made_by_a_trigger_and_backfilled():
    body = _statements(MEDIA_SQL)
    assert "bytes_reserved BIGINT NOT NULL DEFAULT 0 CHECK (bytes_reserved >= 0)" in USAGE_SQL
    assert "asset_count    INTEGER NOT NULL DEFAULT 0 CHECK (asset_count >= 0)" in USAGE_SQL
    assert "INSERT INTO campaign.media_usage (campaign_id) VALUES (NEW.id);" in body
    assert re.search(
        r"CREATE TRIGGER \w+ AFTER INSERT ON campaign\.campaigns\s+FOR EACH ROW EXECUTE FUNCTION "
        r"campaign\.create_media_usage\(\);",
        body,
    )
    assert "INSERT INTO campaign.media_usage (campaign_id) SELECT id FROM campaign.campaigns;" in body


def test_the_media_migration_holds_no_bytes_touches_no_existing_table_and_no_transaction():
    body = _statements(MEDIA_SQL)
    assert "BYTEA" not in body.upper(), "a row holds keys, never bytes"
    assert "ALTER TABLE" not in body.upper(), "no CHECK is added to an existing table"
    assert set(re.findall(r"CREATE TABLE ([\w.]+)", body)) == {"campaign.assets", "campaign.media_usage"}
    assert not re.search(r"\b(BEGIN|COMMIT|END|ROLLBACK)\s*;", body)
    assert "CONCURRENTLY" not in body


def test_the_media_migration_says_why_in_its_own_words():
    """AC-1's five reasons, read where the schema is."""
    prose = " ".join(line.lstrip("- ").strip() for line in MEDIA_SQL.splitlines() if line.startswith("--"))
    for reason in (
        "WHY `campaign_id` IS ON DELETE NO ACTION, WRITTEN OUT.",
        "WHY `media_usage` IS A TABLE OF ITS OWN, NOT COLUMNS ON `campaigns`.",
        "WHY A TRIGGER AND A BACKFILL.",
        "WHY THE KEYS ARE IMMUTABLE.",
        "NO CHECK IS ADDED TO AN EXISTING TABLE",
    ):
        assert reason in prose, reason
    assert "PostgreSQL REFUSES to delete a campaign that still has any asset row" in prose


# ── 1kg.7.1: disclosures and slots ───────────────────────────────────────────


def test_the_reveal_migration_is_the_agreed_text():
    """T-A1. The brief's section 10.2, pinned clause by clause, so that each of
    these changes turns this red: the one-live index losing its predicate, a
    composite key losing a column, a delete action on the slot -> disclosure
    key, a database clock, or a column added to a document table (ED-6)."""
    body = _statements(REVEAL_SQL)
    assert re.findall(r"ALTER TABLE ([\w.]+)\s+ADD CONSTRAINT (\w+) UNIQUE \(id, campaign_id\);", body) == [
        ("campaign.table_sessions", "table_sessions_id_campaign_key"),
        ("campaign.documents", "documents_id_campaign_key"),
    ], "the only change to a released table is the two redundant keys"
    assert len(re.findall(r"\bALTER TABLE\b", body)) == 2
    assert "ADD COLUMN" not in body, "no column on documents or document_versions (ED-6)"
    assert re.search(
        r"CREATE UNIQUE INDEX reveal_disclosures_one_live_per_document_uidx\s+"
        r"ON campaign\.reveal_disclosures \(document_id\) WHERE ended_at IS NULL;",
        body,
    ), "one live disclosure per document, and PARTIAL so that ended_at is not a key"
    assert re.search(
        r"CREATE UNIQUE INDEX reveal_disclosures_command_uidx\s+"
        r"ON campaign\.reveal_disclosures \(session_id, command_id\);",
        body,
    )
    for key in (
        "FOREIGN KEY (session_id, campaign_id)\n    REFERENCES campaign.table_sessions (id, campaign_id) "
        "ON DELETE CASCADE",
        "FOREIGN KEY (document_id, campaign_id)\n    REFERENCES campaign.documents (id, campaign_id) "
        "ON DELETE CASCADE",
        "FOREIGN KEY (document_id, version_number)\n    REFERENCES campaign.document_versions "
        "(document_id, number) ON DELETE CASCADE",
        "FOREIGN KEY (participant_id, campaign_id)\n    REFERENCES campaign.participants (id, campaign_id) "
        "ON DELETE CASCADE",
        "UNIQUE (id, session_id, audience_kind)",
        "UNIQUE NULLS NOT DISTINCT (session_id, participant_id)",
        "CHECK ((audience_kind = 'table') = (participant_id IS NULL))",
        "CHECK ((ended_at IS NULL) = (ended_reason IS NULL))",
        "source_group_id TEXT CHECK (source_group_id IS NULL)",
        "content_kind    TEXT NOT NULL DEFAULT 'document' CHECK (content_kind = 'document')",
    ):
        assert key in body, key
    assert body.count("REFERENCES campaign.table_sessions (id, campaign_id) ON DELETE CASCADE") == 2
    slot_key = re.search(
        r"FOREIGN KEY \(disclosure_id, session_id, audience_kind\)\s+"
        r"REFERENCES campaign\.reveal_disclosures \(id, session_id, audience_kind\)(.*?)\n\);",
        body,
        re.S,
    )
    assert slot_key is not None, "a slot points only at a disclosure of its session and audience kind"
    assert "ON DELETE" not in slot_key.group(1), "NO ACTION: a live copy blocks a document's deletion"
    assert "now()" not in body.lower(), "the clock is the application's"
    assert "CONCURRENTLY" not in body
    assert not re.search(r"(BEGIN|COMMIT|END|ROLLBACK)\s*;", body)
    assert "ended_at >= created_at" not in body.replace(" ", "")


def test_end_reasons_in_python_equal_the_migration_check():
    """T-A5 (text half; `tests/test_reveal_db.py` reads the server's). A reason
    added on one side only is a CheckViolation in the middle of a narrowing."""
    from service.reveal_scope import EndReason

    found = re.search(r"ended_reason\s+TEXT CHECK \(ended_reason IN \(([^)]*)\)\)", REVEAL_SQL, re.S)
    assert found is not None
    in_sql = [value.strip().strip("'") for value in found.group(1).replace("\n", " ").split(",")]
    assert in_sql == [member.value for member in EndReason], "the same set, in the same order"
    assert "link_rotated" in in_sql and "rotated" not in in_sql, "ED-17's spelling"


def test_the_prefix_registry_holds_dsc_and_rsl():
    """T-A30. Both new ids are constrained by the registry's own regex, and the
    command id by the wire contract's."""
    for prefix in (ident.DISCLOSURE, ident.REVEAL_SLOT):
        assert prefix in ident.PREFIXES
        assert f"CHECK (id ~ '{ident.id_check_regex(prefix)}')" in REVEAL_SQL
    assert f"command_id ~ '{COMMAND_ID_PATTERN}'" in REVEAL_SQL
    assert COMMAND_ID_PATTERN == table_session_store.COMMAND_ID.pattern


def test_the_mask_check_is_the_contracts_field_key_bounded_by_mask_max_keys():
    """The mask CHECK's bound and key shape are the wire contract's, so the twin
    (which checks in Python) and the database refuse the same masks."""
    assert f"cardinality(mask) BETWEEN 1 AND {workbench_contracts.MASK_MAX_KEYS}" in REVEAL_SQL
    assert reveal_store.FIELD_KEY.pattern == "^[a-z][a-z0-9_]{0,39}$"
    assert "array_to_string(mask, ',') ~ '^[a-z][a-z0-9_]{0,39}(,[a-z][a-z0-9_]{0,39})*$'" in REVEAL_SQL
    assert "array_position(mask, NULL) IS NULL" in REVEAL_SQL
    assert "strpos(array_to_string(mask, ''), ',') = 0" in REVEAL_SQL, "no comma inside a key"
    assert "NOT ('all' = ANY (mask))" in REVEAL_SQL


def test_the_reveal_migration_states_its_three_proofs():
    prose = " ".join(line.lstrip("- ").strip() for line in REVEAL_SQL.splitlines() if line.startswith("--"))
    for claim in (
        "It is an expansion",
        "The two UNIQUE constraints cannot fail and change no lock mode",
        "No deployed build has a campaign schema",
        "FOR NO KEY UPDATE",
    ):
        assert claim in prose, claim


# ── Field eligibility and groups (bead 1ir.2.1) ──────────────────────────────

#: Found by name, never by number (R-7).
[ELIGIBILITY_SQL_PATH] = sorted(MIGRATIONS.glob("*_field_eligibility_and_groups.sql"))
ELIGIBILITY_SQL = ELIGIBILITY_SQL_PATH.read_text(encoding="utf-8")
FIELD_KEY_PATTERN = get_args(workbench_contracts.FieldKey)[1].pattern


def test_the_eligibility_migration_spells_the_field_key_rule_as_the_wire_does():
    """T-B1: the key CHECK on both tables is `FieldKey`'s pattern and refuses
    every reserved word; db.py's spelling is the wire's; the command-id CHECK
    is `CommandId`'s. Kills drift in any one spelling (M-B1)."""
    reserved = " AND ".join(f"field_key <> '{word}'" for word in sorted(workbench_contracts.RESERVED_MASK_KEYS))
    rule = f"CHECK (field_key ~ '{FIELD_KEY_PATTERN}' AND {reserved})"
    assert _statements(ELIGIBILITY_SQL).count(rule) == 2
    assert db.FIELD_KEY_PATTERN == FIELD_KEY_PATTERN
    assert db.RESERVED_FIELD_KEYS == workbench_contracts.RESERVED_MASK_KEYS
    assert f"created_command_id ~ '{COMMAND_ID_PATTERN}'" in ELIGIBILITY_SQL


def test_the_principal_patterns_are_the_registrys():
    """T-B2 (M-B2): each list class is checked against its prefix's regex."""
    found = re.findall(r"WHEN '(\w+)'\s+THEN campaign\.principal_ids_ok\(principal_ids, '([^']+)'\)", ELIGIBILITY_SQL)
    assert dict(found) == {
        field_class.value: ident.id_check_regex(prefix) for field_class, prefix in eligibility_store.LIST_PREFIX.items()
    }
    assert f"CHECK (id ~ '{ident.id_check_regex(ident.GROUP)}')" in ELIGIBILITY_SQL


def test_the_bounds_the_eligibility_migration_checks_are_the_modules():
    """T-B3 (M-B3): 100 ids, a 40-character name and a 200-character key, and
    64 projection items per advance, each the number its module holds."""
    assert eligibility_store.PRINCIPAL_IDS_MAX == workbench_contracts.PRESENCE_MAX_PARTICIPANTS
    assert f"cardinality(ids) BETWEEN 1 AND {eligibility_store.PRINCIPAL_IDS_MAX}" in ELIGIBILITY_SQL
    assert f"length(name) BETWEEN 1 AND {participant_store.ALIAS_MAX_CHARS}" in ELIGIBILITY_SQL
    assert f"length(name_fold) BETWEEN 1 AND {participant_store.ALIAS_KEY_MAX}" in ELIGIBILITY_SQL
    assert db.PROJECTION_ITEMS_MAX == workbench_contracts.MAX_CHANGED_FIELDS


def test_the_eligibility_migration_states_why_each_check_on_an_existing_table_is_safe():
    """T-B4, the 0009 precedent (M-B4): the one change to an existing table
    says why it is safe, there is no backfill, and no transaction control;
    `unclassified` and `suggested` are never storable. The documents key the
    composite foreign keys target is the reveal migration's, which sorts first,
    so this file adds no second copy of it."""
    prose = " ".join(line.lstrip("- ").strip() for line in ELIGIBILITY_SQL.splitlines() if line.startswith("--"))
    for reason in (
        "THE CHECK IS SAFE ON AN EXISTING TABLE",
        "NO KEY IS ADDED TO campaign.documents",
        "NO BACKFILL",
        "ACCESS EXCLUSIVE",
        "Rollback is sending traffic back to the previous image",
    ):
        assert reason in prose, reason
    body = _statements(ELIGIBILITY_SQL)
    assert not re.search(r"\b(BEGIN|COMMIT|END|ROLLBACK)\s*;", body)
    assert "UPDATE " not in body.upper(), "no existing row is rewritten"
    assert set(re.findall(r"ALTER TABLE ([\w.]+)", body)) == {"campaign.authz_state"}
    assert "ADD CONSTRAINT documents_id_campaign_key" not in body
    assert "ADD CONSTRAINT documents_id_campaign_key UNIQUE (id, campaign_id);" in _statements(REVEAL_SQL)
    assert REVEAL_SQL_PATH.name < ELIGIBILITY_SQL_PATH.name, "the key exists before the foreign keys name it"
    assert "'unclassified'" not in body and "'suggested'" not in body
    assert "CREATE TABLE campaign.characters" not in body, "a character is a character-sheet document (R-6)"
