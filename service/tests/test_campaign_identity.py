"""
Identifiers and secret digests for the campaign domain (1kg.2.1, checkpoint A).

Behaviours 1, 2 and 3 of `plans/drafts/1kg.2.1-campaign-schema.md`. Nothing
here touches a database: these are the pure rules SEC-4 and SEC-5 fix, and
they are the rules both the migration's `CHECK` constraints and the stores rely on.
"""

from __future__ import annotations

import pytest

from service.campaign_identity import (
    CAMPAIGN,
    DEVICE_CREDENTIAL,
    DIGEST_CHARS,
    DOCUMENT,
    ENROLMENT_CODE,
    ID_BODY_MAX,
    ID_BODY_MIN,
    PARTICIPANT,
    PREFIXES,
    SECRET_CHARS,
    TABLE_CREDENTIAL,
    TABLE_SESSION,
    check_id,
    digest,
    id_check_regex,
    is_id,
    new_id,
    new_secret,
)

#: `OpaqueId` of the wire contract: `^[A-Za-z0-9_-]{1,64}$`. Nothing this module
#: mints may fall outside it, because these ids travel on the wire.
OPAQUE_ID_MAX = 64


# ── Behaviour 1: a minted id carries its prefix and 128 bits ─────────────────


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_minted_id_carries_its_prefix_and_at_least_128_bits(prefix):
    minted = new_id(prefix)

    assert minted.startswith(prefix)
    body = minted.removeprefix(prefix)
    # token_urlsafe(16) is 22 base64url characters for 16 bytes = 128 bits, the
    # floor SEC-4 sets. Asserting the length is how we assert the entropy.
    assert len(body) >= ID_BODY_MIN
    assert len(minted) <= OPAQUE_ID_MAX, "an id must fit the wire contract's OpaqueId"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_minted_id_is_url_safe_and_never_sequential(prefix):
    minted = [new_id(prefix) for _ in range(200)]

    for value in minted:
        body = value.removeprefix(prefix)
        assert all(c.isalnum() or c in "-_" for c in body), f"{prefix} body is not URL-safe"
    # Not sequential, and not repeating: 128 bits makes a collision in 200 draws
    # impossible in practice, so a duplicate means a non-random source (SEC-4).
    assert len(set(minted)) == len(minted)


def test_every_prefix_is_distinct_and_the_registry_is_the_whole_set():
    assert len(set(PREFIXES)) == len(PREFIXES)
    assert set(PREFIXES) == {
        CAMPAIGN,
        PARTICIPANT,
        ENROLMENT_CODE,
        DEVICE_CREDENTIAL,
        TABLE_SESSION,
        TABLE_CREDENTIAL,
        DOCUMENT,
    }


# ── Behaviour 2: a wrong or short id is refused ──────────────────────────────


def test_an_id_of_the_wrong_prefix_is_refused():
    a_participant = new_id(PARTICIPANT)

    assert is_id(PARTICIPANT, a_participant)
    assert not is_id(CAMPAIGN, a_participant)
    with pytest.raises(ValueError, match="campaign identifier"):
        check_id(CAMPAIGN, a_participant)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "cmp_",
        "cmp_tooshort",
        "cmp_" + "a" * (ID_BODY_MIN - 1),
        "cmp_" + "a" * (ID_BODY_MAX + 1),
        "CMP_" + "a" * ID_BODY_MIN,
        "cmp_" + "a" * (ID_BODY_MIN - 1) + "!",
        "cmp_" + "a" * (ID_BODY_MIN - 1) + " ",
        "cmp_" + "a" * (ID_BODY_MIN - 1) + "/",
        " cmp_" + "a" * ID_BODY_MIN,
        "cmp_" + "a" * ID_BODY_MIN + "\n",
    ],
)
def test_a_malformed_id_is_refused(value):
    assert not is_id(CAMPAIGN, value)
    with pytest.raises(ValueError):
        check_id(CAMPAIGN, value)


def test_check_id_returns_the_value_so_it_can_be_used_inline():
    minted = new_id(CAMPAIGN)
    assert check_id(CAMPAIGN, minted) == minted


def test_a_refusal_names_the_kind_and_never_repeats_the_value():
    """An id is not a secret, but a refusal that echoes its input is how a
    malformed *secret* eventually gets logged by a caller that reuses the
    message. The message names the kind only (SEC-20)."""
    # Long enough to pass the length bound, but `!` is not in the body alphabet —
    # so this is refused for its shape, and the message must not echo it.
    malformed = "cmp_this-is-not-a-valid-identifier-at-all!!"

    with pytest.raises(ValueError) as caught:
        check_id(CAMPAIGN, malformed)

    assert malformed not in str(caught.value)
    assert "cmp_this-is-not" not in str(caught.value)


def test_an_unknown_prefix_is_a_programming_error():
    with pytest.raises(ValueError, match="not a campaign-domain prefix"):
        new_id("xyz_")
    with pytest.raises(ValueError, match="not a campaign-domain prefix"):
        check_id("xyz_", "xyz_" + "a" * ID_BODY_MIN)


# ── Behaviour 2 (second half): the regex the migration uses is this one ──────


@pytest.mark.parametrize("prefix", PREFIXES)
def test_the_sql_check_regex_accepts_what_the_checker_accepts(prefix):
    """`0004_campaign_schema.sql` constrains each id column with this regex. One
    rule spelled twice drifts, so the SQL is generated from here and
    `test_campaign_schema_sql.py` asserts the file still matches."""
    import re

    pattern = re.compile(id_check_regex(prefix))
    minted = new_id(prefix)

    assert pattern.fullmatch(minted)
    assert not pattern.fullmatch(minted.upper())
    assert not pattern.fullmatch(minted + "!")
    assert not pattern.fullmatch(minted.removeprefix(prefix))


def test_the_sql_check_regex_is_anchored_and_bounded():
    regex = id_check_regex(CAMPAIGN)
    assert regex.startswith("^cmp_")
    assert regex.endswith("$")
    assert f"{{{ID_BODY_MIN},{ID_BODY_MAX}}}" in regex


# ── Behaviour 3: secrets are digested, and neither half is printable ─────────


def test_a_minted_secret_is_43_url_safe_characters():
    minted = new_secret()

    assert len(minted.secret) == SECRET_CHARS
    assert all(c.isalnum() or c in "-_" for c in minted.secret)


def test_only_a_sha256_hex_digest_is_kept_and_it_matches_the_secret():
    minted = new_secret()

    assert len(minted.digest) == DIGEST_CHARS
    assert all(c in "0123456789abcdef" for c in minted.digest)
    assert minted.digest == digest(minted.secret)


def test_two_secrets_never_collide():
    assert len({new_secret().secret for _ in range(200)}) == 200


def test_neither_the_secret_nor_its_digest_appears_in_repr():
    """SEC-20: a secret is private text, and a digest of one is the lookup key
    for a credential. A dataclass that prints its fields puts both into every
    traceback that happens to carry the object."""
    minted = new_secret()

    printed = repr(minted)
    assert minted.secret not in printed
    assert minted.digest not in printed
    assert "MintedSecret" in printed


def test_digesting_is_stable_and_case_sensitive():
    assert digest("abc") == digest("abc")
    assert digest("abc") != digest("ABC")


def test_a_digest_of_an_empty_secret_is_refused():
    """Nothing in this domain has an empty secret, and hashing '' would give a
    well-known digest that matches any row a bug left empty."""
    with pytest.raises(ValueError, match="secret"):
        digest("")
