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

STORE_MODULES = (campaign_store, participant_store, table_session_store, audit_log)


# ── The identifier rule, spelled once ────────────────────────────────────────


def test_every_minted_prefix_is_constrained_in_the_migration_by_the_registrys_own_regex():
    for prefix in ident.PREFIXES:
        expected = ident.id_check_regex(prefix)
        assert f"CHECK (id ~ '{expected}')" in CAMPAIGN_SQL, (
            f"0004 does not constrain {prefix!r} with {expected!r}"
        )


def test_the_migration_constrains_no_identifier_the_registry_has_never_heard_of():
    """The other direction: a CHECK for a prefix nobody mints is a column the
    application can never fill, and a prefix retired from the registry would
    leave one behind."""
    in_sql = set(re.findall(r"CHECK \(id ~ '\^([a-z]{3}_)", CAMPAIGN_SQL))
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
]


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
    if name == "0005":
        assert digests == [], "the audit ledger stores no digest of anything (ED-26)"
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


def test_the_conversation_columns_are_the_four_agreed_and_carry_no_cascade():
    """Requirement 5, and the fail-closed half of it: until 1kg.2.6 decides the
    deletion order, deleting a campaign that still has conversations is refused
    rather than silently detaching or destroying them."""
    added = set(re.findall(r"ADD COLUMN (\w+)", CONVERSATION_SQL))
    assert added == {"campaign_id", "title", "updated_at", "archived_at"}
    assert "mode" not in added, "a mode is per turn, not per conversation"

    reference = re.search(r"campaign_id\s+TEXT REFERENCES campaign\.campaigns \(id\)(.*)", CONVERSATION_SQL)
    assert reference is not None
    assert "ON DELETE" not in reference.group(1), "the campaign reference takes no delete action"


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
