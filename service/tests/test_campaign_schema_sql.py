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

import pytest

from service import audit_log, campaign_store, participant_store, table_session_store
from service import campaign_identity as ident

MIGRATIONS = Path(__file__).resolve().parents[1] / "sql" / "migrations"
CAMPAIGN_SQL = (MIGRATIONS / "0004_campaign_schema.sql").read_text(encoding="utf-8")
AUDIT_SQL = (MIGRATIONS / "0005_audit_events.sql").read_text(encoding="utf-8")
CONVERSATION_SQL = (MIGRATIONS / "0006_conversation_metadata.sql").read_text(encoding="utf-8")
DOCUMENT_SQL = (MIGRATIONS / "0007_document_schema.sql").read_text(encoding="utf-8")

#: Every migration, sorted and concatenated. The two identifier tests below read
#: THIS rather than one file: the prefix registry is service-wide, so a prefix
#: constrained in any migration satisfies it and a prefix nobody mints is a
#: leftover wherever it sits. Reading one file made adding a table in a later
#: migration break a test that had nothing to say about it.
ALL_SQL = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql"))
)

STORE_MODULES = (campaign_store, participant_store, table_session_store, audit_log)


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
]

#: Files that deliberately store no digest at all, each for its own reason, and
#: for which the digest test below asserts the ABSENCE rather than dropping the
#: guard: 0005 because an audit row carries ids and codes and no hash of
#: anything (ED-26), 0007 because a document holds content and not secrets.
NO_DIGEST_FILES = {
    "0005": "the audit ledger stores no digest of anything (ED-26)",
    "0007": "a document holds field content, never a secret (SEC-20)",
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


# ── Digests only, in output ──────────────────────────────────────────────────

#: The five methods that are allowed to hand a caller a secret in plain text —
#: the moment it is minted, and the only moment it exists. Each returns it
#: beside a record whose stored form is the digest. `rotate_link` is here
#: because retiring a generation and minting the next one is a single act
#: (SEC-9), so the new link leaves with the same call that revoked the old.
MINTING_METHODS = {
    "issue_code",
    "issue_device_credential",
    "start",
    "issue_credential",
    "rotate_link",
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
