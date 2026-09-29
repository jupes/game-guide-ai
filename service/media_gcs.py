"""The object store on Cloud Storage (agent-forge-harness-1kg.8.1.4, slice d of
the media bead).

**What this is.** A third implementation of `service.media_objects.ObjectStore`
(media ADR MS-10), beside the in-memory and filesystem stores, and held to the
same contract suite (`service/tests/test_media_objects.py`). It knows keys and
bytes and nothing else: the key grammar is checked before any call reaches the
bucket, and no method takes or returns a filename, a campaign or a cue.

**The client is imported only when it is chosen** (Q-5, AC 11(iii)). Importing
this module imports no cloud library. `build_gcs_store` imports
`google.cloud.storage` inside itself, so a deployment with no media store, or
with the filesystem store, never loads the client, never authenticates and never
reaches a bucket. A build without the library answers `MediaStoreNotBuilt` by
name. Credentials are never in code or configuration: the client finds the
runtime service account through Application Default Credentials.

**How each method maps onto the bucket.**

* `put_stream` is one resumable upload, fed in `upload_chunk_bytes` pieces from
  a bounded source. The upload is finalized only by a short read, which the
  source gives only once the caller's stream has ended within its ceiling. The
  moment the count passes the ceiling the source raises instead, so a body over
  its ceiling is never finalized and nothing appears at the key. The abandoned
  session holds no object; Cloud Storage discards it after a week.
* `get_stream` reads the object's metadata once, then fetches ranges of at most
  `CHUNK_BYTES`, each pinned to the generation it first saw, so a stream never
  mixes two versions of a key. A generation that has gone is `ObjectMissing`.
* `stat_object` is a metadata read; `delete_object` treats "not found" as done,
  because a deletion job is retried until the store agrees (MS-3).
* `list_objects` maps the ordered, paged listing of L-11(a) onto Cloud Storage's
  lexicographic listing and its inclusive `start_offset`: the key equal to the
  cursor is skipped, at most `limit` objects are examined, and a name outside
  the key grammar is never examined, returned or used as a cursor.
* `reachable` lists at most one object under `tmp/`. The runtime service account
  holds object administration on the bucket and nothing more
  (`docs/deploy-gcp.md` section 13), so a bucket read would be refused where an
  object listing is not. For the same reason the builder turns off the client's
  own background read of the bucket's metadata (it fetches the project number
  and location for trace attributes): the store sends no request it did not ask
  for.

**Every call is bounded** by a per-request timeout and a retry deadline inside
a job's budget (`service/job_driver.py`). The retry deadline is checked between
attempts, so an attempt that starts just before it still runs to its own
connect and read timeouts: one store call overruns a handler's advisory
`JobContext` deadline by about `RETRY_DEADLINE_S` plus `REQUEST_TIMEOUT`
(about 33 s) at most, far inside the job lease (300 s). The handlers already
check that deadline before each call. **A failure carries no
driver text.** Cloud Storage's own messages name the bucket and the object, so
every client error is replaced, outside the handler, by the store's named error
with its fixed message; "not found" is the only status read from it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .media_objects import (
    CHUNK_BYTES,
    InvalidObjectKey,
    MediaStoreNotBuilt,
    ObjectMissing,
    ObjectPage,
    ObjectStat,
    ObjectStoreUnavailable,
    ObjectTooLarge,
    _check_ceiling,
    _check_listing,
    _check_window,
    check_key,
    check_prefix,
)

#: One resumable upload request carries this much (a multiple of 256 KiB, as
#: the service requires). It is also the most an upload holds in memory at once.
UPLOAD_CHUNK_BYTES = 4 * CHUNK_BYTES
_UPLOAD_CHUNK_MULTIPLE = 256 * 1024
#: (connect, read) seconds for one HTTP request to the bucket.
REQUEST_TIMEOUT: tuple[float, float] = (3.0, 10.0)
#: The most one store call spends retrying, whatever it is. Inside
#: `job_driver.JOB_BUDGET_S` (30 s) and far inside the job lease (300 s).
RETRY_DEADLINE_S = 20.0
#: Objects carry no type of their own: the asset row holds the type the server
#: recorded, and the byte route (slice c) sends that one (SEC-19).
OBJECT_CONTENT_TYPE = "application/octet-stream"
#: A listing asks only for what it reads.
_LISTING_FIELDS = "items(name,updated),nextPageToken"
#: The client's own switch for its background bucket-metadata read
#: (`google.cloud.storage._opentelemetry_tracing`). Left on, the first call on a
#: bucket starts a thread that asks for `storage.buckets.get`, which the runtime
#: account does not hold.
NO_BUCKET_METADATA_READ = "DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA"
#: The client library, found or imported by its module name only.
_CLIENT_MODULE = "google.cloud.storage"
_NOT_BUILT = "this build does not include the Cloud Storage client (the gcs extra)"

_NOT_FOUND = 404
_PRECONDITION_FAILED = 412

Timeout = float | tuple[float, float]


# ── The part of the client this store uses ───────────────────────────────────


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...

    def tell(self) -> int: ...


class GcsBlob(Protocol):
    """The few attributes and calls of `google.cloud.storage.Blob` used here."""

    @property
    def name(self) -> str | None: ...

    @property
    def size(self) -> int | None: ...

    @property
    def generation(self) -> int | None: ...

    @property
    def updated(self) -> datetime | None: ...

    def upload_from_file(
        self, file_obj: _Readable, *, content_type: str, timeout: Timeout, retry: object
    ) -> None: ...

    def download_as_bytes(
        self,
        *,
        start: int,
        end: int,
        raw_download: bool,
        if_generation_match: int,
        timeout: Timeout,
        retry: object,
    ) -> bytes: ...


class GcsBucket(Protocol):
    """The few calls of `google.cloud.storage.Bucket` used here."""

    def blob(self, blob_name: str, *, chunk_size: int | None = None) -> GcsBlob: ...

    def get_blob(self, blob_name: str, *, timeout: Timeout, retry: object) -> GcsBlob | None: ...

    def delete_blob(self, blob_name: str, *, timeout: Timeout, retry: object) -> None: ...

    def list_blobs(
        self,
        *,
        prefix: str,
        start_offset: str | None = None,
        max_results: int | None = None,
        page_size: int | None = None,
        fields: str,
        timeout: Timeout,
        retry: object,
    ) -> Iterable[GcsBlob]: ...


def _status(error: BaseException) -> int | None:
    """The HTTP status a client error carries, if any. `google.api_core`'s
    errors carry it as `code`; nothing else of the error is ever read."""
    code = getattr(error, "code", None)
    return int(code) if isinstance(code, int) else None


# ── The bounded source an upload reads ───────────────────────────────────────


class _BoundedSource:
    """The caller's chunks as the file object a resumable upload reads.

    `read(n)` answers exactly `n` bytes until the stream ends, because the
    upload takes a short read as "this is the last chunk" and finalizes on it.
    It pulls from the caller one chunk at a time and holds at most `n` bytes
    plus one chunk. **The moment the count passes the ceiling it raises**, so a
    body over its ceiling never gives the short read that would finalize it.
    What went wrong is recorded here, because the upload may re-wrap what it
    sees on its way out.
    """

    def __init__(self, chunks: Iterable[bytes], ceiling: int) -> None:
        self._chunks = iter(chunks)
        self._ceiling = ceiling
        self._pending = bytearray()
        self._handed = 0
        self._ended = False
        self.pulled = 0
        self.too_large = False
        self.source_error: Exception | None = None

    def tell(self) -> int:
        return self._handed

    def read(self, size: int = -1, /) -> bytes:
        while not self._ended and (size < 0 or len(self._pending) < size):
            self._pull()
        take = len(self._pending) if size < 0 else min(size, len(self._pending))
        piece = bytes(self._pending[:take])
        del self._pending[:take]
        self._handed += len(piece)
        return piece

    def _pull(self) -> None:
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._ended = True
            return
        except Exception as error:
            self.source_error = error
            raise
        self.pulled += len(chunk)
        if self.pulled > self._ceiling:
            self.too_large = True
            raise ObjectTooLarge()
        self._pending += chunk


# ── The store ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Found:
    size: int
    generation: int
    modified_at: datetime


class CloudStorageObjectStore:
    """One bucket. See the module docstring for how each method maps onto it.

    `bucket` is a `google.cloud.storage.Bucket`, or anything with the calls
    `GcsBucket` names. `retry` is handed to every call that retries; `None`
    turns retrying off.
    """

    def __init__(
        self,
        bucket: GcsBucket,
        *,
        timeout: Timeout = REQUEST_TIMEOUT,
        retry: object = None,
        upload_chunk_bytes: int = UPLOAD_CHUNK_BYTES,
    ) -> None:
        if (
            not isinstance(upload_chunk_bytes, int)
            or upload_chunk_bytes < _UPLOAD_CHUNK_MULTIPLE
            or upload_chunk_bytes % _UPLOAD_CHUNK_MULTIPLE
        ):
            raise ValueError("an upload chunk is a positive multiple of 256 KiB")
        self._bucket = bucket
        self._timeout = timeout
        self._retry = retry
        self._upload_chunk = upload_chunk_bytes

    def put_stream(self, key: str, chunks: Iterable[bytes], *, max_bytes: int) -> int:
        checked = check_key(key)
        source = _BoundedSource(chunks, _check_ceiling(max_bytes))
        failed = False
        try:
            blob = self._bucket.blob(checked, chunk_size=self._upload_chunk)
            blob.upload_from_file(
                source, content_type=OBJECT_CONTENT_TYPE, timeout=self._timeout, retry=self._retry
            )
        except Exception:
            failed = True
        # Decided from what the source saw, not from what the upload re-raised,
        # and raised outside the handler so that no driver text is attached.
        if source.too_large:
            raise ObjectTooLarge()
        if source.source_error is not None:
            if isinstance(source.source_error, OSError):
                raise ObjectStoreUnavailable()
            raise source.source_error
        if failed:
            raise ObjectStoreUnavailable()
        return source.pulled

    def get_stream(self, key: str, *, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        checked = check_key(key)
        start, wanted = _check_window(offset, length)
        found = self._find(checked)
        if found is None:
            raise ObjectMissing()
        end = found.size if wanted is None else min(found.size, start + wanted)
        return self._read(checked, found.generation, start, end)

    def _read(self, key: str, generation: int, start: int, end: int) -> Iterator[bytes]:
        blob = self._bucket.blob(key)
        at = start
        while at < end:
            stop = min(end, at + CHUNK_BYTES)
            failure: type[ObjectMissing | ObjectStoreUnavailable] | None = None
            chunk = b""
            try:
                chunk = blob.download_as_bytes(
                    start=at,
                    end=stop - 1,
                    raw_download=True,
                    if_generation_match=generation,
                    timeout=self._timeout,
                    retry=self._retry,
                )
            except Exception as error:
                gone = _status(error) in (_NOT_FOUND, _PRECONDITION_FAILED)
                failure = ObjectMissing if gone else ObjectStoreUnavailable
            if failure is None and len(chunk) != stop - at:
                failure = ObjectStoreUnavailable
            if failure is not None:
                raise failure()
            at = stop
            yield chunk

    def _find(self, key: str) -> _Found | None:
        unavailable = False
        blob: GcsBlob | None = None
        try:
            blob = self._bucket.get_blob(key, timeout=self._timeout, retry=self._retry)
        except Exception:
            unavailable = True
        if unavailable:
            raise ObjectStoreUnavailable()
        if blob is None:
            return None
        size, generation, updated = blob.size, blob.generation, blob.updated
        if size is None or generation is None or updated is None:
            raise ObjectStoreUnavailable()
        return _Found(size, generation, updated)

    def stat_object(self, key: str) -> ObjectStat | None:
        found = self._find(check_key(key))
        return None if found is None else ObjectStat(found.size, found.modified_at)

    def delete_object(self, key: str) -> None:
        checked = check_key(key)
        unavailable = False
        try:
            self._bucket.delete_blob(checked, timeout=self._timeout, retry=self._retry)
        except Exception as error:
            unavailable = _status(error) != _NOT_FOUND
        if unavailable:
            raise ObjectStoreUnavailable()

    def list_objects(
        self, prefix: str, *, older_than: datetime, start_after: str | None = None, limit: int
    ) -> ObjectPage:
        under = check_prefix(prefix)
        after = None if start_after is None else check_key(start_after)
        moment, bound = _check_listing(older_than, limit)
        examined: list[str] = []
        old: list[str] = []
        more = False
        unavailable = False
        try:
            listing = self._bucket.list_blobs(
                prefix=under,
                start_offset=after,
                page_size=bound + 2,
                fields=_LISTING_FIELDS,
                timeout=self._timeout,
                retry=self._retry,
            )
            for blob in listing:
                name = blob.name
                if name is None or (after is not None and name <= after) or not _is_key_under(name, under):
                    continue
                if len(examined) == bound:
                    more = True
                    break
                examined.append(name)
                updated = blob.updated
                if updated is not None and updated < moment:
                    old.append(name)
        except Exception:
            unavailable = True
        if unavailable:
            raise ObjectStoreUnavailable()
        last = examined[-1] if examined else None
        return ObjectPage(tuple(old), len(examined), last, last if more else None)

    def reachable(self) -> bool:
        try:
            for _ in self._bucket.list_blobs(
                prefix="tmp/", max_results=1, fields=_LISTING_FIELDS, timeout=self._timeout, retry=None
            ):
                break
        except Exception:
            return False
        return True


def _is_key_under(name: str, prefix: str) -> bool:
    """Whether a listed name is a key this store could have written under
    `prefix`. Anything else in the bucket is never examined."""
    try:
        check_key(name)
    except InvalidObjectKey:
        return False
    return name.startswith(prefix)


# ── Building it ──────────────────────────────────────────────────────────────


def check_client_installed() -> None:
    """Refuses, by name, a build without the client, and imports nothing of it:
    `find_spec` loads only the namespace packages above it. Startup asks this
    for `gcs` (slice b's rule that a setting which cannot be used stops
    startup), because the builder below runs only when the stores are built,
    and a database that is away at startup defers that to recovery."""
    found = None
    try:
        found = importlib.util.find_spec(_CLIENT_MODULE)
    except (ImportError, ValueError):
        found = None
    if found is None:
        raise MediaStoreNotBuilt(_NOT_BUILT)


def build_gcs_store(bucket_name: str) -> CloudStorageObjectStore:
    """The store on `bucket_name`, with the runtime's own credentials. Imports
    the client here and nowhere else; a build without it is refused by name."""
    missing = False
    try:
        # By module name, so that what `sys.modules` says is what happens.
        storage = importlib.import_module(_CLIENT_MODULE)
        retries = importlib.import_module(f"{_CLIENT_MODULE}.retry")
    except ImportError:
        missing = True
    if missing:
        raise MediaStoreNotBuilt(_NOT_BUILT)
    os.environ.setdefault(NO_BUCKET_METADATA_READ, "true")
    client = storage.Client()
    return CloudStorageObjectStore(
        client.bucket(bucket_name),
        timeout=REQUEST_TIMEOUT,
        retry=retries.DEFAULT_RETRY.with_timeout(RETRY_DEADLINE_S),
    )
