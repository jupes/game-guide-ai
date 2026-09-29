"""Where media bytes live: the object store, its keys and its settings
(agent-forge-harness-1kg.8.1.1, slice a of the media bead).

**What an object store is.** One `ObjectStore` protocol and three
implementations of it (media ADR MS-10): `InMemoryObjectStore` for tests and the
end-to-end app, `FilesystemObjectStore` under `WORKBENCH_MEDIA_DIR` for Compose
and E2E, and `service.media_gcs.CloudStorageObjectStore` on the bucket named by
`WORKBENCH_MEDIA_BUCKET` for production (slice d, `1kg.8.1.4`). The factory
imports that module, and through it the Cloud Storage client, only when `gcs`
is chosen. A store knows keys and bytes and nothing else: no method takes or
returns a filename, a campaign, a document or a cue (requirement 2.2).

**What a key is, and what it is not** (MS-2, SEC-28). A key is
`tmp/<32 hex>` for an upload in flight, `assets/<32 hex>` for a processed
original, or `assets/<32 hex>/<name>` for a derivative `1kg.8.2` will make. The
32 hex characters are 16 CSPRNG bytes minted independently of every input
(`service/asset_store.new_object_key`), so a key says nothing: not whose it is,
not what it shows, not what it was called. The asset row is the only map from
an owner to a key. **One grammar**, checked here before any I/O, so that `..`, a
backslash, a NUL, an encoded separator, an absolute path, upper-case hex or any
other prefix is refused before a path is ever built — by one fixed message that
never repeats what it refused.

**The listing is ordered and paged** (L-11(a)). `list_objects` walks the keys
under a prefix in byte order, strictly after a cursor, examines at most `limit`
of them, and returns the examined keys older than a moment plus the last key it
examined — or `None` once nothing is left. The bound counts objects examined,
not matches, so a page of young or still-referenced objects still moves the
cursor on: a reconcile that walks it ends.

**One door** (requirement 2.5). Every call of a store method from outside this
module goes through `via_store`, which is where slice c puts MS-10's thread
limiter; `service/tests/test_media_objects.py` checks every module by `ast`.
The asset store imports nothing from here at all, so no transaction there can
hold a connection while bytes move (requirement 3.8).

**Off by default** (Q-5, L-12). `MediaSettings.from_env` reads two switches, and
the directory or bucket the chosen store needs; the running service reads them
once, at startup, through `startup_settings` (slice b, `service/app.py`).
`memory` is never selectable from the environment — a deployment on it would
lose bytes between instances — and is built in code only.
"""

from __future__ import annotations

import os
import re
import secrets
import stat as stat_mode
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

#: Chunks of at most 256 KiB (*suggested*, MS-7): a slow phone holds a request
#: slot, not a thread and not the file.
CHUNK_BYTES = 256 * 1024

TMP_PREFIX = "tmp/"
ASSETS_PREFIX = "assets/"
#: The two key shapes a row holds, as the media migration's CHECKs spell them. One rule spelled
#: twice drifts, so `service/tests/test_media_objects.py` holds the migration's
#: text to these.
OBJECT_KEY_PATTERN = r"^assets/[0-9a-f]{32}$"
TMP_KEY_PATTERN = r"^tmp/[0-9a-f]{32}$"
#: Reserved for `1kg.8.2`: what is derived from a processed original.
DERIVATIVE_KEY_PATTERN = r"^assets/[0-9a-f]{32}/[a-z0-9][a-z0-9_-]{0,31}$"
_KEYS = tuple(re.compile(p) for p in (OBJECT_KEY_PATTERN, TMP_KEY_PATTERN, DERIVATIVE_KEY_PATTERN))
_DERIVATIVE_PREFIX = re.compile(r"^assets/[0-9a-f]{32}/$")
_DERIVATIVE = re.compile(DERIVATIVE_KEY_PATTERN)

#: The store methods, by name: the chokepoint check looks for these.
STORE_METHODS = ("put_stream", "get_stream", "stat_object", "delete_object", "list_objects", "reachable")


# ── Refusals ─────────────────────────────────────────────────────────────────


class ObjectStoreError(Exception):
    """What an object store refuses or cannot do. Every message is fixed: none
    repeats a key, a path or anything a caller supplied."""

    MESSAGE = "the object store refused that"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class InvalidObjectKey(ObjectStoreError, ValueError):
    """Not a key, a prefix or a cursor this store accepts — refused before any
    I/O (L-11(b))."""

    MESSAGE = "not an object key this store accepts"


class ObjectTooLarge(ObjectStoreError):
    """The stream passed its ceiling. Raised the moment the count passes it,
    and nothing is left at the key."""

    MESSAGE = "the object is larger than its ceiling"


class ObjectMissing(ObjectStoreError, LookupError):
    MESSAGE = "no object at that key"


class ObjectStoreUnavailable(ObjectStoreError):
    """The store could not do its part — an operating-system error, a bucket
    that does not answer. RT-9's `503 backend_unavailable` once a route maps it
    (slices b and c); the driver's own text, which names a path, is never
    attached."""

    MESSAGE = "the object store is unavailable"


class PathEscapesStore(ObjectStoreUnavailable):
    """A key's path resolved outside the store's root — a symlink somewhere in
    the tree. The layout is broken, so the store is unavailable; nothing is
    read or written."""

    MESSAGE = "the object store's layout leads outside its root"


class MediaSettingsError(ValueError):
    """A media setting that cannot be used. The message names the variable and
    never repeats its value."""


class MediaStoreNotBuilt(MediaSettingsError):
    """`WORKBENCH_MEDIA_STORE` names Cloud Storage, and this build does not
    include its client (the `gcs` extra). Refused by name, when the store is
    built, rather than by falling back to anything."""


# ── Keys ─────────────────────────────────────────────────────────────────────


def check_key(key: object) -> str:
    """`key` if it is one of the three shapes, else the one fixed refusal."""
    if isinstance(key, str) and any(pattern.fullmatch(key) for pattern in _KEYS):
        return key
    raise InvalidObjectKey()


def check_prefix(prefix: object) -> str:
    """A listing walks `tmp/`, `assets/`, or one original's derivatives."""
    if isinstance(prefix, str) and (
        prefix in (TMP_PREFIX, ASSETS_PREFIX) or _DERIVATIVE_PREFIX.fullmatch(prefix)
    ):
        return prefix
    raise InvalidObjectKey()


def parent_key(key: str) -> str:
    """The key a derivative hangs off; any other key is its own parent."""
    checked = check_key(key)
    if _DERIVATIVE.fullmatch(checked):
        return checked.rsplit("/", 1)[0]
    return checked


def _check_ceiling(max_bytes: object) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("an object's ceiling is a positive whole number of bytes")
    return max_bytes


def _check_window(offset: object, length: object) -> tuple[int, int | None]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("a read's offset is a whole number of bytes from 0")
    if length is not None and (isinstance(length, bool) or not isinstance(length, int) or length < 0):
        raise ValueError("a read's length is a whole number of bytes from 0, or None")
    return offset, length


def _check_listing(older_than: object, limit: object) -> tuple[datetime, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("a listing's limit is a positive whole number")
    if not isinstance(older_than, datetime) or older_than.tzinfo is None or older_than.utcoffset() is None:
        raise ValueError("a listing's moment is timezone-aware")
    return older_than, limit


# ── The protocol ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObjectStat:
    size: int
    modified_at: datetime


@dataclass(frozen=True)
class ObjectPage:
    """One page of a listing: the examined keys older than the moment asked
    for, how many were examined, the last key examined (None if none was), and
    the cursor — that same last key while more remain, `None` once the listing
    is exhausted."""

    keys: tuple[str, ...]
    examined: int
    last: str | None
    cursor: str | None


class ObjectStore(Protocol):
    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        """Write `chunks` to `key` and return the byte count. The moment the
        count passes `max_bytes`, stop pulling, leave nothing at the key, and
        raise `ObjectTooLarge`. The whole body is never held."""
        ...  # pragma: no cover - structural type

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        """The bytes of `key` from `offset`, at most `length` of them, in chunks
        of at most `CHUNK_BYTES`. A missing key raises `ObjectMissing` at the
        call; a window past the end yields nothing."""
        ...  # pragma: no cover - structural type

    def stat_object(self, key: str) -> ObjectStat | None:
        ...  # pragma: no cover - structural type

    def delete_object(self, key: str) -> None:
        """Idempotent: deleting what is not there is success, because a
        deletion job is retried until the store agrees (MS-3)."""
        ...  # pragma: no cover - structural type

    def list_objects(
        self, prefix: str, *, older_than: datetime, start_after: str | None = None, limit: int
    ) -> ObjectPage:
        """See the module docstring: ordered, paged, bounded by what it examines."""
        ...  # pragma: no cover - structural type

    def reachable(self) -> bool:
        """Read-only: whether the store answers. `1kg.9.2` surfaces it as
        `/healthz`'s `media.store` (RT-9)."""
        ...  # pragma: no cover - structural type


def via_store[T](store: ObjectStore, call: Callable[[ObjectStore], T]) -> T:
    """The one door to an object store from outside this module (requirement
    2.5). Slice c puts MS-10's thread limiter here, and with it MS-7's ceiling
    on concurrent byte responses; until then it only hands the call over."""
    return call(store)


def _page(ordered: list[str], start_after: str | None, limit: int) -> tuple[list[str], str | None]:
    """The examined window of a sorted key list, and its cursor."""
    remaining = [key for key in ordered if start_after is None or key > start_after]
    examined = remaining[:limit]
    cursor = examined[-1] if len(remaining) > limit else None
    return examined, cursor


# ── In memory ────────────────────────────────────────────────────────────────


class InMemoryObjectStore:
    """For tests and the end-to-end app. Built in code only: a deployment on it
    would lose every byte at the next restart (L-12)."""

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        #: key -> (bytes, last written).
        self._objects: dict[str, tuple[bytes, datetime]] = {}

    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        checked = check_key(key)
        ceiling = _check_ceiling(max_bytes)
        parts: list[bytes] = []
        total = 0
        for chunk in chunks:
            total += len(chunk)
            if total > ceiling:
                raise ObjectTooLarge()
            parts.append(bytes(chunk))
        self._objects[checked] = (b"".join(parts), self._clock())
        return total

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        checked = check_key(key)
        start, wanted = _check_window(offset, length)
        found = self._objects.get(checked)
        if found is None:
            raise ObjectMissing()
        data = found[0]
        end = len(data) if wanted is None else min(len(data), start + wanted)
        return (data[at : min(end, at + CHUNK_BYTES)] for at in range(start, end, CHUNK_BYTES))

    def stat_object(self, key: str) -> ObjectStat | None:
        found = self._objects.get(check_key(key))
        return None if found is None else ObjectStat(len(found[0]), found[1])

    def delete_object(self, key: str) -> None:
        self._objects.pop(check_key(key), None)

    def list_objects(
        self, prefix: str, *, older_than: datetime, start_after: str | None = None, limit: int
    ) -> ObjectPage:
        under = check_prefix(prefix)
        after = None if start_after is None else check_key(start_after)
        moment, bound = _check_listing(older_than, limit)
        ordered = sorted(key for key in self._objects if key.startswith(under))
        examined, cursor = _page(ordered, after, bound)
        old = tuple(key for key in examined if self._objects[key][1] < moment)
        return ObjectPage(old, len(examined), examined[-1] if examined else None, cursor)

    def reachable(self) -> bool:
        return True


# ── On disk ──────────────────────────────────────────────────────────────────

#: What the filesystem store's own directories hold, and nothing else is read
#: back as a key: a temporary file starts with a dot and matches none of these.
_OBJECT_FILE = re.compile(r"^([0-9a-f]{32})\.o$")
_DERIVATIVE_DIR = re.compile(r"^([0-9a-f]{32})\.d$")
_DERIVATIVE_FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class FilesystemObjectStore:
    """A directory under `WORKBENCH_MEDIA_DIR`, for Compose and E2E (MS-10).

    **The map from keys to paths is fixed and one-to-one**, because a file and a
    directory cannot share a name: `tmp/<hex>` is `tmp/<hex>.o`, `assets/<hex>` is
    `assets/<hex>.o`, and `assets/<hex>/<name>` is `assets/<hex>.d/<name>`. An
    original and its derivatives therefore coexist, and each deletes on its own.
    A key's `/` is never handed to the operating system as a separator.

    **A write is published only when it is whole**: the bytes go to a
    temporary name in the same directory and `os.replace` puts them at the key
    on success, so a failed or oversized write leaves neither the key nor the
    temporary file. **Every path is resolved and must stay under the resolved
    root**, so a symlink cannot lead a key out of it. An operating-system error
    other than "not found" is `ObjectStoreUnavailable`, with a fixed message.

    A listing sorts the directory for each page: this store serves Compose and
    E2E only, where that is cheap.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._resolved_root = Path(os.path.realpath(self._root))

    def _path(self, key: str) -> Path:
        checked = check_key(key)
        space, name = checked.split("/", 1)
        if "/" in name:
            original, derivative = name.split("/", 1)
            return self._root / space / f"{original}.d" / derivative
        return self._root / space / f"{name}.o"

    def _contained(self, path: Path) -> Path:
        """`path` if it resolves under the root, else the named refusal."""
        if not Path(os.path.realpath(path)).is_relative_to(self._resolved_root):
            raise PathEscapesStore()
        return path

    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        target = self._path(key)
        ceiling = _check_ceiling(max_bytes)
        self._contained(target)
        # Same directory, so `os.replace` is a rename; a leading dot, so no
        # listing ever reads it back as a key.
        temporary = target.parent / f".{secrets.token_hex(8)}.part"
        too_large = unavailable = False
        total = 0
        try:
            os.makedirs(target.parent, exist_ok=True)
            try:
                with open(temporary, "xb") as out:
                    for chunk in chunks:
                        total += len(chunk)
                        if total > ceiling:
                            too_large = True
                            break
                        out.write(chunk)
                if not too_large:
                    os.replace(temporary, target)
            finally:
                # After a replace there is nothing left to remove; after
                # anything else this is what keeps a failed write from leaving
                # a partial file behind.
                _remove_quietly(temporary)
        except OSError:
            unavailable = True
        # Raised outside the handler, so the driver's error — which names a
        # path — is not attached to what a caller may log.
        if unavailable:
            raise ObjectStoreUnavailable()
        if too_large:
            raise ObjectTooLarge()
        return total

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        path = self._path(key)
        start, wanted = _check_window(offset, length)
        size = self._size(path)
        if size is None:
            raise ObjectMissing()
        end = size if wanted is None else min(size, start + wanted)
        return self._read(path, start, end)

    def _read(self, path: Path, start: int, end: int) -> Iterator[bytes]:
        if start >= end:
            return
        failure: type[ObjectStoreError] | None = None
        try:
            with open(path, "rb") as source:
                source.seek(start)
                at = start
                while at < end:
                    chunk = source.read(min(CHUNK_BYTES, end - at))
                    if not chunk:
                        break
                    at += len(chunk)
                    yield chunk
        except FileNotFoundError:
            failure = ObjectMissing
        except OSError:
            failure = ObjectStoreUnavailable
        if failure is not None:
            raise failure()

    def _size(self, path: Path) -> int | None:
        found = self._stat(path)
        return None if found is None else found.st_size

    def _stat(self, path: Path) -> os.stat_result | None:
        self._contained(path)
        unavailable = False
        try:
            found = os.stat(path)
        except FileNotFoundError:
            return None
        except OSError:
            unavailable = True
        if unavailable:
            raise ObjectStoreUnavailable()
        if not stat_mode.S_ISREG(found.st_mode):
            return None
        return found

    def stat_object(self, key: str) -> ObjectStat | None:
        found = self._stat(self._path(key))
        if found is None:
            return None
        return ObjectStat(found.st_size, datetime.fromtimestamp(found.st_mtime, UTC))

    def delete_object(self, key: str) -> None:
        path = self._path(key)
        self._contained(path)
        unavailable = False
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            unavailable = True
        if unavailable:
            raise ObjectStoreUnavailable()
        if path.parent.name.endswith(".d"):
            # The last derivative takes its directory with it; a directory that
            # still holds one, or is already gone, stays as it is.
            _remove_directory_quietly(path.parent)

    def list_objects(
        self, prefix: str, *, older_than: datetime, start_after: str | None = None, limit: int
    ) -> ObjectPage:
        under = check_prefix(prefix)
        after = None if start_after is None else check_key(start_after)
        moment, bound = _check_listing(older_than, limit)
        examined, cursor = _page(sorted(self._keys_under(under)), after, bound)
        old: list[str] = []
        for key in examined:
            found = self._stat(self._path(key))
            if found is not None and datetime.fromtimestamp(found.st_mtime, UTC) < moment:
                old.append(key)
        return ObjectPage(tuple(old), len(examined), examined[-1] if examined else None, cursor)

    def _keys_under(self, prefix: str) -> list[str]:
        if prefix == TMP_PREFIX:
            return [f"tmp/{m.group(1)}" for m in self._matching(self._root / "tmp", _OBJECT_FILE)]
        if prefix == ASSETS_PREFIX:
            keys: list[str] = []
            for entry in self._entries(self._root / "assets"):
                if (found := _OBJECT_FILE.fullmatch(entry)) is not None:
                    keys.append(f"assets/{found.group(1)}")
                elif (found := _DERIVATIVE_DIR.fullmatch(entry)) is not None:
                    keys.extend(self._derivatives(found.group(1)))
            return keys
        return self._derivatives(prefix.split("/")[1])

    def _derivatives(self, original: str) -> list[str]:
        directory = self._root / "assets" / f"{original}.d"
        return [f"assets/{original}/{name}" for name in self._entries(directory) if _DERIVATIVE_FILE.fullmatch(name)]

    def _matching(self, directory: Path, pattern: re.Pattern[str]) -> list[re.Match[str]]:
        return [m for m in (pattern.fullmatch(entry) for entry in self._entries(directory)) if m is not None]

    def _entries(self, directory: Path) -> list[str]:
        self._contained(directory)
        unavailable = False
        names: list[str] = []
        try:
            with os.scandir(directory) as found:
                names = [entry.name for entry in found]
        except FileNotFoundError:
            return []
        except OSError:
            unavailable = True
        if unavailable:
            raise ObjectStoreUnavailable()
        return names

    def reachable(self) -> bool:
        try:
            return os.path.isdir(self._resolved_root) and os.access(self._resolved_root, os.R_OK | os.W_OK)
        except OSError:
            return False


def _remove_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _remove_directory_quietly(path: Path) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass


# ── Settings, and the factory ────────────────────────────────────────────────

#: `WORKBENCH_MEDIA_ENABLED`'s documented values. Anything else is refused.
_TRUE = frozenset({"true", "1"})
_FALSE = frozenset({"false", "0", ""})
#: What `WORKBENCH_MEDIA_STORE` may name from the environment. `memory` is not
#: here: it is built in code only.
_FROM_ENVIRONMENT = frozenset({"filesystem", "gcs"})
#: A bucket name as `docs/deploy-gcp.md` section 13 makes one: lower-case
#: letters, digits, `-` and `_`, 3 to 63 characters, starting and ending with a
#: letter or a digit. Dotted (domain) bucket names are not accepted.
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,61}[a-z0-9]$")
_GCS = "gcs"
_MEMORY = "memory"
_FILESYSTEM = "filesystem"


@dataclass(frozen=True)
class MediaSettings:
    """The media capability's two switches, in `service/db.py`'s settings shape
    and never in `config.py` (L-12).

    `enabled` gates the routes (slice b) and is off by default (Q-5). `store`
    selects the backend and is `None` by default — no store is built, so there
    is no bucket, no client and no cost. The two are separate on purpose: a
    deployment switched off after having been on still needs its store to
    finish its deletions (MS-3).
    """

    enabled: bool = False
    #: None, "filesystem", "gcs" or, in code only, "memory".
    store: str | None = None
    media_dir: Path | None = None
    #: The Cloud Storage bucket, for `gcs` only.
    bucket: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MediaSettings:
        source = os.environ if env is None else env
        raw_enabled = source.get("WORKBENCH_MEDIA_ENABLED", "")
        if raw_enabled in _TRUE:
            enabled = True
        elif raw_enabled in _FALSE:
            enabled = False
        else:
            raise MediaSettingsError("WORKBENCH_MEDIA_ENABLED must be one of: true, false, 1, 0")
        raw_store = source.get("WORKBENCH_MEDIA_STORE", "")
        if raw_store == "":
            return cls(enabled=enabled)
        if raw_store not in _FROM_ENVIRONMENT:
            raise MediaSettingsError("WORKBENCH_MEDIA_STORE must be unset or name a store this build provides")
        if raw_store == _GCS:
            return cls(enabled=enabled, store=_GCS, bucket=_check_bucket(source.get("WORKBENCH_MEDIA_BUCKET", "")))
        directory = source.get("WORKBENCH_MEDIA_DIR", "")
        if not directory or not Path(directory).is_absolute():
            raise MediaSettingsError("WORKBENCH_MEDIA_DIR must name an absolute directory for this store")
        return cls(enabled=enabled, store=_FILESYSTEM, media_dir=Path(directory))


def _check_bucket(name: object) -> str:
    if isinstance(name, str) and _BUCKET_NAME.fullmatch(name):
        return name
    raise MediaSettingsError("WORKBENCH_MEDIA_BUCKET must name a Cloud Storage bucket for this store")


def startup_settings(env: Mapping[str, str] | None = None) -> MediaSettings:
    """What the running service reads at startup (slice b, `1kg.8.1.2`):
    `MediaSettings.from_env`, plus the one rule only a running service needs —
    the routes cannot be switched on with no store to put bytes in. Refused by
    name at startup, so a misconfigured deployment fails loudly (AC 11)."""
    settings = MediaSettings.from_env(env)
    if settings.enabled and settings.store is None:
        raise MediaSettingsError("WORKBENCH_MEDIA_ENABLED needs WORKBENCH_MEDIA_STORE to name a store")
    return settings


def build_object_store(
    settings: MediaSettings, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> ObjectStore | None:
    """The configured store, or `None` when none is. Builds whatever is
    configured, whatever `enabled` says: the capability gates the routes and
    never the deletions a store still owes (MS-3). Only `gcs` imports the
    Cloud Storage client, and only here."""
    if settings.store is None:
        return None
    if settings.store == _MEMORY:
        return InMemoryObjectStore(clock=clock)
    if settings.store == _FILESYSTEM:
        if settings.media_dir is None or not settings.media_dir.is_absolute():
            raise MediaSettingsError("WORKBENCH_MEDIA_DIR must name an absolute directory for this store")
        return FilesystemObjectStore(settings.media_dir)
    if settings.store == _GCS:
        bucket = _check_bucket(settings.bucket)
        from .media_gcs import build_gcs_store

        return build_gcs_store(bucket)
    raise MediaSettingsError("WORKBENCH_MEDIA_STORE must be unset or name a store this build provides")
