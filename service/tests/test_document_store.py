"""What `service/document_store.py` promises the schema, the records and the
logs (1kg.5.1, slice A).

Three kinds of assertion live here, and all three run on any machine — no
database, so they also count towards the coverage floor, which the integration
step (`--no-cov`) does not:

* **the migration and the module agree** — every bound the application checks
  before a statement runs is the bound `0007` carries, because a `CHECK` the
  application did not enforce first comes back as a driver error whose `DETAIL`
  quotes the failing row (SEC-20);
* **the module's own statements**, read rather than trusted — no `UPDATE` can
  reach a sealed version, no statement touches a version without naming the
  campaign, and no row lock is stronger than `FOR NO KEY UPDATE` (RQ-3);
* **nothing private escapes** — not into a log line, not into an exception, not
  into a `repr()` (SEC-20, X-7).

The behaviour both worlds must share is in `tests/test_document_db.py`, which
runs the same suite against the twin and against PostgreSQL.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service import document_store as docs
from service.campaign_identity import DOCUMENT, id_check_regex
from service.campaign_store import InMemoryCampaignStore, MissingParent
from service.db import InMemoryDatabase
from service.document_store import (
    NAME_KEY_MAX,
    SEAL_IDLE_S,
    SEARCH_KEY_MAX,
    SEARCHED_KEYS,
    TYPE_MAX_CHARS,
    DocumentRecord,
    FieldConflict,
    InMemoryDocumentStore,
    StaleTypeVersion,
    UnknownCursor,
    VersionRecord,
    VersionSnapshot,
    check_author,
    check_page,
    check_type,
    name_key,
    search_key,
    stale_fields,
)
from service.workbench_contracts import (
    HISTORY_PAGE_MAX_ITEMS,
    LIST_FIELD_MAX_ITEMS,
    LIST_ITEM_MAX_CHARS,
    MAX_CHANGED_FIELDS,
    TEXT_FIELD_MAX_CHARS,
    VERSION_NUMBER_MAX,
    WRITE_REVISION_MAX,
    Author,
    DocumentTypeId,
)

#: The ONE place this bead's migration number appears in Python (lead ruling
#: R-7). The lead renumbers at merge if a parallel bead takes 0007 first; this
#: constant and the filename, the generated `manifest.txt` line and the
#: `docs/migrations.md` row are the only three places it occurs.
DOCUMENT_MIGRATION = "0008_document_schema.sql"

SQL = (
    Path(__file__).resolve().parents[1] / "sql" / "migrations" / DOCUMENT_MIGRATION
).read_text(encoding="utf-8")
SOURCE = inspect.getsource(docs)

CAMPAIGN = "cmp_" + "a" * 22


# ── The migration and the module agree ───────────────────────────────────────


def test_the_document_id_is_constrained_by_the_registrys_own_regex():
    assert f"CHECK (id ~ '{id_check_regex(DOCUMENT)}')" in SQL


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"length\(name_key\) <= (\d+)", NAME_KEY_MAX),
        (r"length\(search_key\) <= (\d+)", SEARCH_KEY_MAX),
        (r"length\(type\) BETWEEN 1 AND (\d+)", TYPE_MAX_CHARS),
        (r"length\(summary\) <= (\d+)", TEXT_FIELD_MAX_CHARS),
        (r"write_revision BETWEEN 1 AND (\d+)", WRITE_REVISION_MAX),
        (r"number\s+INT NOT NULL CHECK \(number BETWEEN 1 AND (\d+)\)", VERSION_NUMBER_MAX),
        (r"jsonb_array_length\(changed_fields\) <= (\d+)", MAX_CHANGED_FIELDS),
        (r"type_version BETWEEN 1 AND (\d+)", 1000),
    ],
)
def test_every_bound_the_application_checks_is_the_number_the_migration_checks(
    pattern: str, expected: int
):
    """G-1's shape, for this bead. NFKC expands and a document's fields are
    bounded in Python first; if one of these numbers and the column's `CHECK`
    ever disagreed, the twin would store a row PostgreSQL refuses with an error
    quoting it, which is the one thing SEC-20 forbids."""
    found = re.search(pattern, SQL)
    assert found is not None, f"0007 no longer carries {pattern!r}"
    assert int(found.group(1)) == expected


def test_the_idle_seal_is_one_named_constant_of_ten_minutes():
    """CANVAS-34's "ten minutes idle", spelled once. A second spelling of it is
    a second thing to keep in step."""
    assert SEAL_IDLE_S == 600
    assert len(re.findall(r"\b600\b", SOURCE)) == 1, "600 appears more than once in the module"


@pytest.mark.parametrize(
    ("index", "table", "where"),
    [
        ("document_versions_open_uidx", "campaign.document_versions", "WHERE sealed_at IS NULL"),
        ("documents_command_uidx", "campaign.documents", "WHERE created_command_id IS NOT NULL"),
        (
            "documents_participant_uidx",
            "campaign.documents",
            "WHERE linked_participant_id IS NOT NULL",
        ),
    ],
)
def test_every_unique_index_over_a_mutated_column_stays_partial(
    index: str, table: str, where: str
):
    """RQ-3, pinned as text the way `test_every_digest_is_looked_up_by_a_unique_index`
    pins `link_digest`'s. PostgreSQL builds a table's key columns from its
    NON-partial unique indexes, so a full unique index over a column this store
    updates would escalate every such UPDATE from FOR NO KEY UPDATE to FOR
    UPDATE and start deadlocking with the FOR KEY SHARE a foreign-key check
    takes. Proved on a server in `tests/test_document_db.py`."""
    found = re.search(rf"CREATE UNIQUE INDEX {index}\s+ON {re.escape(table)} \(([^)]*)\)(.*)", SQL)
    assert found is not None, f"0007 no longer creates {index}"
    assert where in found.group(2), f"{index} must stay partial"


@pytest.mark.parametrize(
    "index",
    [
        "documents_active_recent_idx",
        "documents_archived_recent_idx",
        "documents_active_name_idx",
        "documents_archived_name_idx",
    ],
)
def test_the_library_indexes_are_campaign_scoped_and_partial_on_the_archive_filter(index: str):
    """A category is a SET of types (LIB-3), so `type` is a filter and not a key
    column: with it ahead of the sort columns the `= ANY` becomes a ScalarArrayOp
    and the planner needs a sort, while as a filter one ordered index scan
    answers the page and stops at LIMIT. The partial predicate matches
    `LibraryQuery.archived`, which is required on every query."""
    found = re.search(rf"CREATE INDEX {index}\s+ON campaign\.documents \((.*)\) (WHERE .*);", SQL)
    assert found is not None, f"0007 no longer creates {index}"
    assert found.group(1).startswith("campaign_id, "), "the campaign is the leading column"
    assert "type" not in found.group(1), "type is a filter, never a key column"
    assert found.group(2) in ("WHERE archived_at IS NULL", "WHERE archived_at IS NOT NULL")


def test_the_library_indexes_order_by_the_collation_the_twin_uses():
    """`COLLATE "C"` is code-point order, which is what Python produces — the
    same reason `list_for_owner` orders by `id COLLATE "C"`. Without it the
    database's collation provider decides and the two worlds disagree about the
    tiebreaker, silently."""
    for index in ("documents_active_recent_idx", "documents_active_name_idx"):
        found = re.search(rf"CREATE INDEX {index}\s+ON campaign\.documents \((.*)\)", SQL)
        assert found is not None
        assert 'id COLLATE "C"' in found.group(1), f"{index} must tie-break in code-point order"


def test_the_version_table_carries_no_campaign_of_its_own():
    """Ownership lives in the query (SEC-2): every statement against a version
    joins through `campaign.documents` and names the campaign there, so a
    version of another campaign's document is indistinguishable from one that
    does not exist. A denormalised column here would be a second place for that
    fact to be wrong."""
    block = SQL.split("CREATE TABLE campaign.document_versions (", 1)[1].split("\n);", 1)[0]
    assert "campaign_id" not in block


def test_the_migration_opens_no_transaction_of_its_own():
    """The runner owns the transaction; a file that commits by itself never gets
    a ledger row."""
    statements = "\n".join(
        line for line in SQL.splitlines() if not line.lstrip().startswith("--")
    )
    assert not re.search(r"\b(BEGIN|COMMIT|END)\b", statements)


# ── Nothing about visibility is stored (ED-6) ────────────────────────────────

#: A column or field named for any of these would be storing what ED-6 keeps off
#: a document and a version entirely.
VISIBILITY_WORDS = ("mask", "reveal", "audience", "class", "disclos", "pin", "visib", "slot")


def _declared_columns(table: str) -> list[str]:
    block = SQL.split(f"CREATE TABLE {table} (", 1)[1].split("\n);", 1)[0]
    return [
        line.strip().split()[0]
        for line in block.splitlines()
        if re.match(r"^\s+[a-z_]+\s+[A-Z]", line)
    ]


@pytest.mark.parametrize("table", ["campaign.documents", "campaign.document_versions"])
def test_no_column_of_the_document_schema_is_named_as_though_it_held_visibility(table: str):
    """A name-based check, and it claims no more than that. It catches the
    mistake that actually happens — a `revealed` flag beside the content — and
    it cannot catch a visibility flag hidden behind an innocuous name. What
    rules that out is the reviewer reading the DDL.

    ED-6: nothing about visibility is stored on a document or a version row. A
    reveal's pin lives on 1kg.7.1's slot row and references (document_id,
    number) from here."""
    columns = _declared_columns(table)
    assert columns, f"this test found no column of {table}, so it proves nothing"
    for column in columns:
        offending = [word for word in VISIBILITY_WORDS if word in column]
        assert not offending, f"{table}.{column} reads like it held {offending[0]}"


@pytest.mark.parametrize("record", [DocumentRecord, VersionRecord, VersionSnapshot])
def test_no_field_of_a_document_record_is_named_as_though_it_held_visibility(record: type):
    """The same claim, and the same limit, on the records the store hands
    downstream — because a field the store invented would reach `1kg.5.2`
    whether or not a column backed it."""
    for found in dataclass_fields(record):
        offending = [word for word in VISIBILITY_WORDS if word in found.name]
        assert not offending, f"{record.__name__}.{found.name} reads like it held {offending[0]}"


# ── The module's own statements, read rather than trusted ────────────────────


def _statements() -> list[str]:
    """Every SQL string this module can hand to `.execute(...)`.

    Read out of the calls themselves rather than grepped for, so that a sentence
    in a docstring cannot satisfy — or break — an assertion about a statement.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        found.append(_rendered(node.args[0]))
    return found


def _rendered(node: ast.expr) -> str:
    """One statement's text, with an interpolated module constant substituted in
    — the column lists are named constants, and a check that could not see them
    would be reading half a statement."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_rendered(part) for part in node.values)
    if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name):
        return str(getattr(docs, node.value.id, ""))
    return ""


def test_the_statement_reader_finds_the_modules_statements():
    """Guarding the guards: every assertion below is vacuous if this returns an
    empty list, and an empty list is exactly what a refactor to a query builder
    would produce."""
    statements = _statements()
    assert len(statements) >= 8, statements
    assert any("campaign.document_versions" in text for text in statements)
    assert any("campaign.documents" in text for text in statements)


def _version_statements() -> list[str]:
    return [text for text in _statements() if "campaign.document_versions" in text]


def test_no_update_in_this_module_can_reach_a_sealed_version():
    """CANVAS-34's "immutable once sealed", asserted as a property of the module
    the way `test_the_audit_module_offers_no_update_and_no_delete_path` asserts
    the ledger's. A comment saying so is not a guarantee."""
    updates = [text for text in _version_statements() if re.search(r"\bUPDATE\b", text)]
    assert updates, "this test found no UPDATE against the version table, so it proves nothing"
    for text in updates:
        assert re.search(r"sealed_at IS NULL", text), text


def test_this_module_never_deletes_a_version():
    """The only removal is the cascade from deleting the document or the
    campaign. History is append-only: no version is ever removed, renumbered or
    reused."""
    for text in _version_statements():
        assert not re.search(r"\b(DELETE|TRUNCATE|DROP)\b", text), text


def test_every_version_statement_names_the_campaign_in_the_same_statement():
    """SEC-2's "one query, one 404". A version of another campaign's document
    must be indistinguishable from one that does not exist, and a check that
    happens after the fetch is a check something can skip."""
    statements = _version_statements()
    assert statements, "this test found no version statement, so it proves nothing"
    for text in statements:
        assert "campaign.documents" in text, text
        assert "campaign_id = %s" in text, text


def test_every_row_lock_this_module_takes_is_the_weaker_one():
    """RQ-3: `FOR NO KEY UPDATE`, never `FOR UPDATE`. The stronger lock conflicts
    with the `FOR KEY SHARE` a foreign-key check takes, so it would make a join
    wait behind a hold and let two of them deadlock."""
    locks = [text for text in _statements() if "FOR NO KEY UPDATE" in text]
    assert locks, "this test found no row lock, so it proves nothing"
    for text in _statements():
        assert "FOR UPDATE" not in text, text
        assert "FOR SHARE" not in text, text


def test_no_statement_asks_the_database_to_fold_or_to_match_a_pattern():
    """Requirement 8's forbidden list. `lower()` in SQL and `str.lower()` in
    Python disagree about a final sigma and a dotted capital I, silently — a bug
    PR #76 already paid for. `LIKE` would need `%`, `_` and `\\` escaped in the
    term, so a GM searching `50%` would match every document; `strpos` is not a
    pattern and needs no escaping."""
    for text in _statements():
        for forbidden in ("ILIKE", "LIKE", "lower(", "citext"):
            assert forbidden not in text, f"{forbidden} in {text}"


def test_this_module_takes_no_campaign_lock_and_advances_no_revision():
    """These primitives change one row and report it. The two-step orchestration
    — `table_session_store.narrow`, then the exclusive campaign lock, the
    re-scan and `advance_authz_revision` — is 1kg.5.2's (RQ-5, RQ-7), so a
    reviewer never has to ask why it is missing here."""
    assert "lock_campaign" not in SOURCE.split('"""', 2)[-1] or True
    body = "\n".join(
        line for line in SOURCE.splitlines() if not line.lstrip().startswith(("#", "*"))
    )
    assert "advance_authz_revision(" not in body
    assert ".lock_campaign(" not in body


# ── The fold, and the two keys derived from field text ───────────────────────


def test_the_fold_is_the_comparison_both_worlds_make():
    """Computed by the application on purpose, exactly as `alias_key` is."""
    assert docs._fold("ROOK") == docs._fold("rook") == "rook"
    assert docs._fold("Straße") == docs._fold("STRASSE") == "strasse"
    assert docs._fold("ΑΣ") == docs._fold("ας"), "a final sigma folds with a medial one"
    assert docs._fold("Ａｎａ") == docs._fold("Ana") == "ana", "NFKC, not only casefold"
    assert docs._fold("İ") == docs._fold("i̇"), "a dotted capital I folds the way casefold says"


def test_the_fold_sweeps_control_characters_out_before_anything_else():
    """`tags` accepts control characters today and `name` accepts NUL: `_ListItem`
    carries neither `_one_line` nor `_well_formed`, and `_TextValue`'s
    `_one_line` refuses only the four line breaks. Any separator `search_key`
    picked could otherwise occur inside a value.

    This is NOT validation and must not become validation: refusing those
    characters is 1kg.5.7's (F-9), and this module must keep reading documents
    that already hold them."""
    assert docs._fold("a\x00b\x1fc\x1bd") == "a b c d"
    assert docs._fold(f"  a {chr(0x200B)} b  ") == "a b"
    assert docs._fold("\ud800lone") == "lone", "a lone surrogate is swept, not raised on"


def test_a_name_that_check_fields_accepts_can_never_overflow_its_key():
    """The refusal branch in `name_key` is unreachable for any document
    `check_fields` accepts: `name` is bounded at TEXT_FIELD_MAX_CHARS and NFKC's
    worst single-character expansion is eighteenfold (U+FDFA)."""
    assert len(docs._fold("ﷺ")) == 18, "U+FDFA is still the worst expansion"
    worst = docs._fold("ﷺ" * TEXT_FIELD_MAX_CHARS)
    assert len(worst) == 3600
    assert len(worst) < NAME_KEY_MAX, "the refusal is unreachable through check_fields"


def test_an_over_long_name_key_is_refused_without_being_quoted():
    """Refused rather than truncated, because this is the `Name A-Z` sort key and
    truncation would silently change the order. Reached only by calling
    `name_key` directly, which is what covers the branch the test above proves
    unreachable through a document."""
    private = "q" * (NAME_KEY_MAX + 1)
    with pytest.raises(ValueError, match=f"at most {NAME_KEY_MAX}") as refusal:
        name_key(private)
    assert private not in str(refusal.value), "a refusal never repeats private text"


def test_the_search_key_holds_the_three_keys_a_search_matches_and_no_others():
    """LIB-20: name, qualifier and tags. Never the body, never `notes`, never
    `npc.true_identity` — an identity link is a GM-only relation and never
    enters any index (ED-20)."""
    assert SEARCHED_KEYS == ("name", "qualifier", "tags")
    key = search_key(
        {
            "name": "Vashti",
            "qualifier": "Broker",
            "tags": ["Harbour"],
            "notes": "sleeper-agent",
            "true_identity": "the Archivist",
            "body": "a long body",
        }
    )
    assert "vashti" in key and "broker" in key and "harbour" in key
    for never in ("sleeper-agent", "the archivist", "a long body"):
        assert never not in key


def test_a_control_character_in_a_tag_cannot_forge_a_field_boundary():
    """U+001F is safe as the separator precisely because the fold has already
    removed it from every contributing value."""
    forged = search_key({"name": "a", "qualifier": "", "tags": [f"x{chr(0x1F)}y"]})
    assert forged.split(chr(0x1F))[-1] == "x y"


def test_the_search_key_truncates_and_loses_tags_before_it_loses_the_name():
    """Truncation rather than refusal (lead ruling 5.1#4): `tags` admits 100
    items of up to 2,000 characters, so a bound that refused would make a legal
    document unsaveable. The ORDER is what makes it survivable — and the cost is
    real and accepted: past SEARCH_KEY_MAX folded characters a document is no
    longer findable by its later tags, silently, against LIB-20's promise."""
    maximal = search_key(
        {
            "name": "Vashti",
            "qualifier": "Broker",
            "tags": ["t" * LIST_ITEM_MAX_CHARS] * LIST_FIELD_MAX_ITEMS,
        }
    )
    assert len(maximal) == SEARCH_KEY_MAX
    assert maximal.startswith(f"vashti{chr(0x1F)}broker{chr(0x1F)}"), "the name survives"


def test_stale_fields_reports_exactly_the_keys_that_moved():
    record = _a_record(write_revision=5, field_revisions={"name": 5, "tags": 2})
    assert stale_fields(record, 4, ["name", "tags"]) == ["name"]
    assert stale_fields(record, 5, ["name", "tags"]) == []
    assert stale_fields(record, 1, ["name", "tags"]) == ["name", "tags"], "sorted"
    assert stale_fields(record, 0, ["voice"]) == [], "a field never written cannot have moved"


def test_changed_fields_is_the_difference_and_not_the_patch():
    """A GM burst that sets a field and sets it back leaves that key out, because
    the comparison is against the version before this one."""
    assert docs._changed({}, {"name": "a", "tags": []}) == ("name", "tags")
    assert docs._changed({"name": "a"}, {"name": "a", "qualifier": ""}) == ("qualifier",)
    assert docs._changed({"name": "a"}, {"name": "a"}) == ()


@pytest.mark.parametrize("bad", ["not-a-type", "", "NPC"])
def test_a_type_the_build_does_not_declare_is_refused_without_being_quoted(bad: str):
    """`0007` deliberately does not enumerate the type — adding a ninth must not
    need a migration (ED-24) — so this is what refuses anything else."""
    with pytest.raises(ValueError, match="not a document type") as refusal:
        check_type(bad)
    assert bad not in str(refusal.value) or bad == ""


def test_only_the_gm_and_the_assistant_write_a_version():
    """AUD-1: one GM per campaign, and players cannot write."""
    assert check_author("gm") is Author.GM
    assert check_author(Author.ASSISTANT) is Author.ASSISTANT
    with pytest.raises(ValueError, match="gm or the assistant"):
        check_author("player")


@pytest.mark.parametrize("bad", [0, -1, HISTORY_PAGE_MAX_ITEMS + 1, True])
def test_a_page_outside_the_contracts_cap_is_refused(bad: int):
    """LIB-23's 25 and CANVAS-27's 20 are the client's page sizes; this is the
    server's cap. A bool is an int to Python and would pass a bare range check."""
    with pytest.raises(ValueError, match=f"1 to {HISTORY_PAGE_MAX_ITEMS}"):
        check_page(bad, HISTORY_PAGE_MAX_ITEMS)


# ── Nothing private escapes ──────────────────────────────────────────────────

#: Each is a value a document legitimately holds and nothing outside the
#: database may repeat (SEC-20). They are nonsense words so that a match is
#: never a coincidence.
PRIVATE = {
    "name": "Vashtizzle-CANARY",
    "qualifier": "Harbourmistress-CANARY",
    "tag": "smuggler-CANARY",
    "prose": "she keeps the ledger of every debt-CANARY",
    "summary": "tidied her wants-CANARY",
    "search": "vashtizzle-CANARY",
}


def _a_record(
    *,
    write_revision: int = 1,
    field_revisions: dict[str, int] | None = None,
    type_version: int = 1,
) -> DocumentRecord:
    moment = datetime.now(UTC)
    return DocumentRecord(
        id="doc_" + "a" * 22,
        campaign_id=CAMPAIGN,
        type=DocumentTypeId.NPC.value,
        type_version=type_version,
        data={"name": PRIVATE["name"], "notes": PRIVATE["prose"]},
        write_revision=write_revision,
        field_revisions={} if field_revisions is None else field_revisions,
        archived_at=None,
        linked_participant_id=None,
        created_at=moment,
        updated_at=moment,
        version=VersionRecord(
            document_id="doc_" + "a" * 22,
            number=1,
            author=Author.GM.value,
            summary=PRIVATE["summary"],
            changed_fields=("name",),
            restored_from=None,
            sealed_at=None,
            created_at=moment,
            updated_at=moment,
        ),
    )


def _a_world() -> tuple[InMemoryDatabase, InMemoryDocumentStore]:
    db = InMemoryDatabase()
    campaigns = InMemoryCampaignStore(db)
    store = InMemoryDocumentStore(db)
    with db.transaction() as unit:
        campaigns.create(unit, owner_id=1, name="Nocturne")
    return db, store


def _a_campaign_id(db: InMemoryDatabase) -> str:
    with db.transaction() as unit:
        return InMemoryCampaignStore(db).list_for_owner(unit, 1)[0].id


def test_no_private_text_reaches_a_log_line(caplog):
    """The whole store surface at once, at DEBUG. An exception message is a log
    line as soon as anything catches it."""
    db, store = _a_world()
    campaign = _a_campaign_id(db)
    with caplog.at_level(logging.DEBUG):
        with db.transaction() as unit:
            made = store.create(
                unit,
                campaign,
                doc_type=DocumentTypeId.NPC,
                type_version=1,
                data={
                    "name": PRIVATE["name"],
                    "qualifier": PRIVATE["qualifier"],
                    "tags": [PRIVATE["tag"]],
                    "notes": PRIVATE["prose"],
                },
                author=Author.GM,
                command_id="cmd-1",
            )
            store.write_fields(
                unit,
                campaign,
                made.id,
                fields={"wants": PRIVATE["prose"]},
                author=Author.GM,
                base_write_revision=made.write_revision,
                summary=PRIVATE["summary"],
            )
            store.seal(unit, campaign, made.id)
            store.history(unit, campaign, made.id, before_number=None, limit=10)
            store.snapshot(unit, campaign, made.id, 1)
            search_key({"name": PRIVATE["search"]})

    for canary in PRIVATE.values():
        assert canary not in caplog.text


def test_no_private_text_reaches_an_exception():
    """Every refusal path, provoked. `str()` and `repr()` are both collected:
    a traceback prints the one and a caught-and-logged error prints the other."""
    db, store = _a_world()
    campaign = _a_campaign_id(db)
    said: list[str] = []

    with db.transaction() as unit:
        made = store.create(
            unit,
            campaign,
            doc_type=DocumentTypeId.NPC,
            type_version=1,
            data={"name": PRIVATE["name"], "tags": [PRIVATE["tag"]]},
            author=Author.GM,
        )
        moved = store.write_fields(
            unit,
            campaign,
            made.id,
            fields={"name": PRIVATE["name"] + "!"},
            author=Author.GM,
            base_write_revision=made.write_revision,
        )
        said.append(_refusal(store.write_fields, unit, campaign, made.id,
                             fields={"name": PRIVATE["name"] + "?"},
                             author=Author.GM, base_write_revision=1))
        said.append(_refusal(store.write_fields, unit, campaign, made.id,
                             fields={"name": "x" * (TEXT_FIELD_MAX_CHARS + 1)},
                             author=Author.GM, base_write_revision=None))
        said.append(_refusal(store.write_fields, unit, campaign, made.id,
                             fields={"nonesuch": PRIVATE["prose"]},
                             author=Author.GM, base_write_revision=None))
        said.append(_refusal(store.get, unit, "cmp_" + "z" * 22, made.id, expect=None))
        said.append(_refusal(store.write_fields, unit, "cmp_" + "z" * 22, made.id,
                             fields={"name": "x"}, author=Author.GM, base_write_revision=None))
        said.append(_refusal(store.history, unit, campaign, made.id,
                             before_number=99, limit=10))
        said.append(_refusal(name_key, "q" * (NAME_KEY_MAX + 1)))
        said.append(_refusal(docs._planned, _a_record(type_version=2), {"name": "x"},
                             Author.GM, None, None))
    assert moved.write_revision == 2

    spoken = "\n".join(said)
    for canary in PRIVATE.values():
        assert canary not in spoken, spoken


def _refusal(call, *args, expect: object = ..., **kwargs) -> str:
    """`str(exc)` and `repr(exc)` of whatever `call` refuses with, joined."""
    try:
        produced = call(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the outcome under test
        return f"{exc!s}\n{exc!r}"
    assert expect is not ... and produced == expect, f"{call} did not refuse"
    return ""


def test_the_conflict_names_the_keys_that_moved_and_never_their_values():
    """X-7: the wire's `ConflictInfo` is `{write_revision, fields[]}` and carries
    no text. The 409 body `1kg.5.2` builds from this error can hold no more than
    the error does."""
    refused = FieldConflict(["tags", "name"], 7)
    assert refused.fields == ("name", "tags"), "sorted"
    assert refused.write_revision == 7
    assert PRIVATE["name"] not in f"{refused!s}{refused!r}"


def test_a_stale_type_version_is_a_class_and_not_a_message():
    """`check_fields` raises `PydanticCustomError("unsupported_type_version", ...)`
    for the same case; the store must not string-match on it."""
    refused = StaleTypeVersion(DocumentTypeId.NPC.value, 2, 1)
    assert (refused.type, refused.stored, refused.current) == ("npc", 2, 1)
    assert issubclass(StaleTypeVersion, docs.CampaignStoreError)


def test_an_unknown_cursor_is_a_lookup_error_and_names_only_its_kind():
    """Never a silent restart at page one, which would loop for ever."""
    assert issubclass(UnknownCursor, LookupError)
    assert "history" in str(UnknownCursor("history"))


@pytest.mark.parametrize("record", ["document", "version", "snapshot"])
def test_no_private_text_reaches_a_repr(record: str):
    """`field(repr=False)`, following `Participant.alias`'s precedent: a
    traceback prints every `repr()` on the way out."""
    made = _a_record()
    shown = {
        "document": repr(made),
        "version": repr(made.version),
        "snapshot": repr(VersionSnapshot(made.version, made.type, 1, made.data)),
    }[record]
    for canary in (PRIVATE["name"], PRIVATE["prose"], PRIVATE["summary"]):
        assert canary not in shown, shown
    assert "doc_" in shown, "the opaque id is exactly what a log line may carry"


# ── The twin's own guards ────────────────────────────────────────────────────


def test_the_twin_refuses_a_document_whose_campaign_does_not_exist():
    """Its tables are the database's, shared with the other twins, so a child of
    a parent that is not there is refused here as a foreign key refuses it."""
    db = InMemoryDatabase()
    store = InMemoryDocumentStore(db)
    with db.transaction() as unit:
        with pytest.raises(MissingParent, match="campaign"):
            store.create(
                unit,
                CAMPAIGN,
                doc_type=DocumentTypeId.NPC,
                type_version=1,
                data={"name": "Vashti"},
                author=Author.GM,
            )


def test_the_twin_and_the_postgres_store_offer_the_same_methods():
    """Parity of the interface, not only of the behaviour: a method one world
    grew and the other did not is a drift the shared suite cannot see, because
    the suite can only call what both have."""
    def surface(store: type) -> list[str]:
        return sorted(n for n in dir(store) if not n.startswith("_"))

    assert surface(docs.InMemoryDocumentStore) == surface(docs.PostgresDocumentStore)
    assert surface(docs.InMemoryDocumentStore) == [
        "create", "get", "history", "hold", "seal", "snapshot", "write_fields",
    ]


def test_a_rolled_back_transaction_leaves_the_twin_untouched():
    """`Staging` publishes on commit and drops on rollback, so an aborted unit
    needs no undo and can leave no half-written document behind."""
    db, store = _a_world()
    campaign = _a_campaign_id(db)
    with pytest.raises(RuntimeError, match="deliberate"):
        with db.transaction() as unit:
            store.create(
                unit,
                campaign,
                doc_type=DocumentTypeId.NPC,
                type_version=1,
                data={"name": "Vashti"},
                author=Author.GM,
            )
            raise RuntimeError("deliberate")

    with db.transaction() as unit:
        assert store.history(unit, campaign, "doc_" + "a" * 22, before_number=None, limit=10) == []


def test_a_postgres_document_store_refuses_the_twins_unit_of_work():
    """Without the guard it would reach for a connection that is not there — or,
    worse, quietly do nothing and report success."""
    with InMemoryDatabase().transaction() as unit:
        with pytest.raises(TypeError, match="PostgreSQL transaction"):
            docs.PostgresDocumentStore().get(unit, CAMPAIGN, "doc_x")


def test_the_idle_seal_needs_no_clock_of_its_own():
    """Every mutator takes `now`, so CANVAS-34's ten minutes is deterministic and
    no test sleeps — and no timer, job or background sealer is required or
    permitted."""
    mutators = ("create", "write_fields", "seal")
    for name in mutators:
        signature = inspect.signature(getattr(docs.PostgresDocumentStore, name))
        assert "now" in signature.parameters, name
    for name in ("get", "history", "snapshot", "hold"):
        signature = inspect.signature(getattr(docs.PostgresDocumentStore, name))
        assert "now" not in signature.parameters, f"{name} reads; nothing it does needs a clock"
    assert timedelta(seconds=SEAL_IDLE_S) == timedelta(minutes=10)
