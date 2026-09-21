"""
Identifiers and secret digests for the campaign domain (1kg.2.1).

Two rules of the GM Workbench threat model live here together, because the values
they govern are minted, checked and stored by the same code paths.

**SEC-4 — identifiers.** A new id is at least 128 bits from the operating system's
CSPRNG, URL-safe, and carries a short type prefix so that a human reading a log
line knows what kind of thing they are looking at. It is never sequential: a
sequential id leaks volume and invites probing. Ids are **not** secrets — every
authorisation rule still applies to the row they name — but they are opaque, and
they must fit the wire contract's ``OpaqueId`` (``^[A-Za-z0-9_-]{1,64}$``).

**SEC-5 — secrets.** An enrolment code, a table link token and a device
credential are each 32 random bytes, and the server stores **only a SHA-256
digest**, looked up by that digest. A slow password hash is deliberately *not*
used here: these are 256-bit random values with nothing to brute-force, and a slow
hash on a route anyone can call is a denial-of-service lever. ``service/hashing.py``
is the other case — human-chosen passwords — and the two must not be confused.

**Why the SQL ``CHECK`` regex is generated from this module.** Every id column in
``0004_campaign_schema.sql`` is constrained to its prefix, and the application
mints the values. One rule spelled twice drifts, so :func:`id_check_regex` is the
single source and the migration is written from it;
``service/tests/test_campaign_schema_sql.py`` asserts the file still agrees.

**AUD-4 — an unused enrolment code expires after seven days.** The window belongs
to this module rather than to the caller, so no route can mint a code that
outlives the rule.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

# ── The prefix registry ──────────────────────────────────────────────────────
#
# Add a member here and the migration's CHECK for it is generated from
# `id_check_regex`; never reuse a prefix, because a retired one may still name
# rows in a backup.

CAMPAIGN: Final = "cmp_"
PARTICIPANT: Final = "prt_"
ENROLMENT_CODE: Final = "enc_"
DEVICE_CREDENTIAL: Final = "dev_"
TABLE_SESSION: Final = "ses_"
TABLE_CREDENTIAL: Final = "tcr_"

PREFIXES: Final[tuple[str, ...]] = (
    CAMPAIGN,
    PARTICIPANT,
    ENROLMENT_CODE,
    DEVICE_CREDENTIAL,
    TABLE_SESSION,
    TABLE_CREDENTIAL,
)

#: What each prefix is called in a refusal. Never the value, only the kind (SEC-20).
_KINDS: Final[dict[str, str]] = {
    CAMPAIGN: "campaign identifier",
    PARTICIPANT: "participant identifier",
    ENROLMENT_CODE: "enrolment-code identifier",
    DEVICE_CREDENTIAL: "device-credential identifier",
    TABLE_SESSION: "table-session identifier",
    TABLE_CREDENTIAL: "table-credential identifier",
}

#: 16 bytes = 128 bits, SEC-4's floor, which `token_urlsafe` renders as 22 characters.
ID_BYTES: Final = 16
ID_BODY_MIN: Final = 22
#: A prefix is 4 characters and `OpaqueId` admits 64, so a body may not exceed 60.
#: The upper bound exists so that a longer id minted by a later build is still
#: refused by the database rather than silently truncated on the wire.
ID_BODY_MAX: Final = 60

#: 32 bytes, rendered as 43 base64url characters (SEC-5).
SECRET_BYTES: Final = 32
SECRET_CHARS: Final = 43
#: SHA-256 as lowercase hex.
DIGEST_CHARS: Final = 64

#: AUD-4: a personal link's code expires seven days after it is issued.
CODE_LIFETIME: Final = timedelta(days=7)

_PREFIX_SHAPE: Final = re.compile(r"^[a-z]{3}_$")
_BODY: Final = re.compile(rf"^[A-Za-z0-9_-]{{{ID_BODY_MIN},{ID_BODY_MAX}}}$")


def _check_prefix(prefix: str) -> str:
    if _PREFIX_SHAPE.fullmatch(prefix) is None or prefix not in PREFIXES:
        # A programming error, not user input: the caller passed something that is
        # not one of this module's kinds.
        raise ValueError(f"{prefix!r} is not a campaign-domain prefix")
    return prefix


def new_id(prefix: str) -> str:
    """Mint an identifier of `prefix`: the prefix plus 128 CSPRNG bits, URL-safe."""
    return _check_prefix(prefix) + secrets.token_urlsafe(ID_BYTES)


def is_id(prefix: str, value: str) -> bool:
    """Whether `value` is a well-formed identifier of `prefix`."""
    _check_prefix(prefix)
    if not value.startswith(prefix):
        return False
    return _BODY.fullmatch(value.removeprefix(prefix)) is not None


def check_id(prefix: str, value: str) -> str:
    """Return `value` if it is a well-formed identifier of `prefix`, else raise.

    The message names the **kind** and never repeats the value: a caller that
    logs the message must not thereby log whatever it was handed (SEC-20).
    """
    if not is_id(prefix, value):
        raise ValueError(f"not a well-formed {_KINDS[prefix]}")
    return value


def id_check_regex(prefix: str) -> str:
    """The POSIX regular expression `0004_campaign_schema.sql` constrains an id
    column with. Generated here so the database and the application cannot
    disagree about what an identifier is."""
    _check_prefix(prefix)
    return rf"^{prefix}[A-Za-z0-9_-]{{{ID_BODY_MIN},{ID_BODY_MAX}}}$"


# ── Secrets ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MintedSecret:
    """A freshly minted secret and the digest to store.

    The plaintext leaves the process exactly once — in the link or code the GM
    sends — and is never persisted. **Both fields are hidden from ``repr()``**:
    the secret is private text, and its digest is the lookup key for a live
    credential, so neither belongs in a log line or a traceback (SEC-20).
    """

    secret: str = field(repr=False)
    digest: str = field(repr=False)


def new_secret() -> MintedSecret:
    """Mint a 32-byte secret and the SHA-256 digest that is stored in its place."""
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return MintedSecret(secret=secret, digest=digest(secret))


def digest(secret: str) -> str:
    """The lowercase hex SHA-256 of `secret` — the only form that is stored.

    An empty secret is refused: nothing in this domain has one, and hashing ``""``
    yields a well-known digest that would match any row a bug had left empty.
    """
    if not secret:
        raise ValueError("a secret is never empty")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ── Expiry ───────────────────────────────────────────────────────────────────


def code_expiry(now: datetime | None = None) -> datetime:
    """When an enrolment code issued at `now` expires (AUD-4: seven days).

    Takes a clock, never a duration, so that no caller can widen the window.
    """
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        # A naive value would be read as UTC by the driver and as local time by
        # everything else — a silent off-by-hours against a TIMESTAMPTZ column.
        raise ValueError("a clock passed here is timezone-aware")
    return moment + CODE_LIFETIME
