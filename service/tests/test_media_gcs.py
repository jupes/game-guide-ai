"""The Cloud Storage object store's own guarantees (agent-forge-harness-1kg.8.1.4).

The contract every store keeps runs in `service/tests/test_media_objects.py`,
over this store too. What is asserted here is what only this store has to get
right, through the real `google-cloud-storage` client against the in-process
emulator of `service/tests/_gcs_emulator.py` (no bucket, no credentials):

* **An upload over its ceiling is never finalized**, so nothing appears at the
  key, and an upload travels in bounded chunks carrying nothing but its bytes.
* **A read asks for bounded ranges pinned to one generation**, so a stream
  never mixes two versions of a key.
* **A failure is the named error with no driver text**: Cloud Storage's own
  messages name the bucket and the object.
* **Keys are checked before any request**, and a listing never examines a name
  outside the key grammar.
* **The production builder** uses the runtime's credentials and bounded calls.

    uv run --frozen --no-sync python -m pytest service/tests/test_media_gcs.py -q
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from service import media_gcs
from service import media_objects as mo
from service.tests._gcs_emulator import BUCKET, Emulator
from service.tests.test_media_objects import _hostile_keys

HEX = "0123456789abcdef" * 2
OTHER_HEX = "fedcba9876543210" * 2
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
FAR_FUTURE = T0 + timedelta(days=36500)
MIB = 1024 * 1024


@dataclass
class Bucket:
    emulator: Emulator
    store: media_gcs.CloudStorageObjectStore


@pytest.fixture
def gcs(monkeypatch: pytest.MonkeyPatch) -> Iterator[Bucket]:
    monkeypatch.setenv(media_gcs.NO_BUCKET_METADATA_READ, "true")
    emulator = Emulator(lambda: T0)
    client = storage.Client(
        project="media-test", credentials=AnonymousCredentials(), client_options={"api_endpoint": emulator.endpoint}
    )
    try:
        yield Bucket(emulator, media_gcs.CloudStorageObjectStore(client.bucket(BUCKET), retry=None))
    finally:
        emulator.close()


def _requests(bucket: Bucket, method: str, path_part: str) -> list[dict[str, list[str]]]:
    return [r.query for r in bucket.emulator.requests if r.method == method and path_part in r.path]


# ── Uploads ──────────────────────────────────────────────────────────────────


def test_a_body_over_its_ceiling_is_never_finalized_and_nothing_appears_at_the_key(gcs: Bucket) -> None:
    """The ceiling lies past two whole upload chunks, so the upload is well
    under way when the count passes it. It stops there: no final request is
    ever sent, and the unfinished session holds no object."""
    pulled = 0

    def endless() -> Iterator[bytes]:
        nonlocal pulled
        while True:
            pulled += 1
            yield b"x" * 100_000

    ceiling = 2 * media_gcs.UPLOAD_CHUNK_BYTES + 50_000
    with pytest.raises(mo.ObjectTooLarge):
        gcs.store.put_stream("tmp/" + HEX, endless(), max_bytes=ceiling)
    assert pulled <= ceiling // 100_000 + 1, "the store read past the ceiling plus one chunk"
    assert "tmp/" + HEX not in gcs.emulator.objects
    [session] = gcs.emulator.sessions.values()
    assert not session.finished
    assert session.puts == [media_gcs.UPLOAD_CHUNK_BYTES] * 2, "only whole chunks went out; no final one"


def test_an_upload_travels_in_bounded_chunks_and_carries_nothing_but_its_bytes(gcs: Bucket) -> None:
    body = bytes(range(256)) * (10 * 1024 + 7)  # a little over 2.5 MiB, handed over in one piece
    assert gcs.store.put_stream("assets/" + HEX, iter([body]), max_bytes=len(body)) == len(body)
    [session] = gcs.emulator.sessions.values()
    assert session.finished
    assert session.puts[:-1] == [media_gcs.UPLOAD_CHUNK_BYTES] * (len(session.puts) - 1)
    assert 0 < session.puts[-1] <= media_gcs.UPLOAD_CHUNK_BYTES and sum(session.puts) == len(body)
    assert session.metadata == {"name": "assets/" + HEX}, "no filename, no custom metadata, nothing but the key"
    assert session.content_type == media_gcs.OBJECT_CONTENT_TYPE
    assert gcs.emulator.objects["assets/" + HEX].data == body


@pytest.mark.parametrize("size", [0, media_gcs.UPLOAD_CHUNK_BYTES, 2 * media_gcs.UPLOAD_CHUNK_BYTES - 1])
def test_an_empty_or_chunk_aligned_body_is_finalized_whole(gcs: Bucket, size: int) -> None:
    """A resumable upload ends on a short read; an exact multiple of the chunk
    ends on an empty one."""
    body = b"q" * size
    halves = iter([body[: size // 2], body[size // 2 :]])
    assert gcs.store.put_stream("tmp/" + HEX, halves, max_bytes=max(size, 1)) == size
    assert gcs.emulator.objects["tmp/" + HEX].data == body


def test_a_failing_source_is_the_callers_error_and_nothing_is_finalized(gcs: Bucket) -> None:
    refused = ValueError("the caller's own failure")

    def failing(error: Exception) -> Iterator[bytes]:
        yield b"a" * (media_gcs.UPLOAD_CHUNK_BYTES + 10)
        raise error

    with pytest.raises(ValueError) as caught:
        gcs.store.put_stream("tmp/" + HEX, failing(refused), max_bytes=10 * MIB)
    assert caught.value is refused
    went_away = ConnectionResetError("the client went away")
    with pytest.raises(mo.ObjectStoreUnavailable) as unavailable:
        gcs.store.put_stream("tmp/" + OTHER_HEX, failing(went_away), max_bytes=10 * MIB)
    assert str(unavailable.value) == mo.ObjectStoreUnavailable.MESSAGE
    assert gcs.emulator.objects == {}
    assert not any(session.finished for session in gcs.emulator.sessions.values())


@pytest.mark.parametrize("chunk", [0, 1000, 256 * 1024 + 1, -256 * 1024, 512.0 * 1024])
def test_the_upload_chunk_is_a_whole_multiple_of_256_kib(chunk: float) -> None:
    with pytest.raises(ValueError, match="256 KiB"):
        media_gcs.CloudStorageObjectStore(_Unused(), upload_chunk_bytes=chunk)  # type: ignore[arg-type]
    assert media_gcs.CloudStorageObjectStore(_Unused(), upload_chunk_bytes=512 * 1024) is not None  # type: ignore[arg-type]


# ── Reads ────────────────────────────────────────────────────────────────────


def test_a_read_asks_for_ranges_of_at_most_one_chunk_pinned_to_one_generation(gcs: Bucket) -> None:
    body = bytes(range(256)) * (3 * mo.CHUNK_BYTES // 256) + b"tail!"
    gcs.emulator.put("assets/" + HEX, body)
    generation = gcs.emulator.objects["assets/" + HEX].generation
    gcs.emulator.reset_log()
    assert b"".join(gcs.store.get_stream("assets/" + HEX, offset=7, length=len(body))) == body[7:]
    downloads = [r for r in gcs.emulator.requests if r.path.startswith("/download/")]
    assert len(downloads) == 3, "3 chunks less 2 bytes, from byte 7: three ranges"
    for request in downloads:
        first, last = (int(n) for n in request.headers["range"].removeprefix("bytes=").split("-"))
        assert last - first + 1 <= mo.CHUNK_BYTES
        assert request.query["ifGenerationMatch"] == [str(generation)]


def test_a_stream_whose_object_is_replaced_mid_read_ends_missing_never_mixed(gcs: Bucket) -> None:
    gcs.emulator.put("assets/" + HEX, b"1" * (2 * mo.CHUNK_BYTES))
    stream = gcs.store.get_stream("assets/" + HEX)
    assert next(stream) == b"1" * mo.CHUNK_BYTES
    gcs.emulator.put("assets/" + HEX, b"2" * (2 * mo.CHUNK_BYTES))
    with pytest.raises(mo.ObjectMissing):
        next(stream)


def test_an_empty_window_makes_no_download_request(gcs: Bucket) -> None:
    gcs.emulator.put("tmp/" + HEX, b"abc")
    gcs.emulator.reset_log()
    assert list(gcs.store.get_stream("tmp/" + HEX, offset=3)) == []
    assert list(gcs.store.get_stream("tmp/" + HEX, offset=1, length=0)) == []
    assert not any(r.path.startswith("/download/") for r in gcs.emulator.requests)


# ── Failures carry no driver text ─────────────────────────────────────────────


def _put_body(store: mo.ObjectStore) -> object:
    return store.put_stream("tmp/" + OTHER_HEX, iter([b"z" * (2 * media_gcs.UPLOAD_CHUNK_BYTES)]), max_bytes=10 * MIB)


FAILURES: list[tuple[str, str, str, Callable[[mo.ObjectStore], object]]] = [
    ("stat", "GET", "/storage/v1/b/", lambda s: s.stat_object("tmp/" + HEX)),
    ("open", "GET", "/storage/v1/b/", lambda s: s.get_stream("tmp/" + HEX)),
    ("read", "GET", "/download/", lambda s: b"".join(s.get_stream("tmp/" + HEX))),
    ("delete", "DELETE", "/storage/v1/b/", lambda s: s.delete_object("tmp/" + HEX)),
    ("list", "GET", "/storage/v1/b/", lambda s: s.list_objects("tmp/", older_than=FAR_FUTURE, limit=5)),
    ("start-upload", "POST", "/upload/", _put_body),
    ("upload-chunk", "PUT", "/upload/", _put_body),
]


@pytest.mark.parametrize(("operation", "method", "path_part", "call"), FAILURES, ids=[f[0] for f in FAILURES])
@pytest.mark.parametrize("status", [503, 403])
def test_every_client_failure_is_the_named_error_with_no_driver_text(
    gcs: Bucket, operation: str, method: str, path_part: str, call: Callable[[mo.ObjectStore], object], status: int
) -> None:
    gcs.emulator.put("tmp/" + HEX, b"present")
    gcs.emulator.fail(method, path_part, status=status)
    with pytest.raises(mo.ObjectStoreUnavailable) as caught:
        call(gcs.store)
    assert type(caught.value) is mo.ObjectStoreUnavailable
    assert str(caught.value) == mo.ObjectStoreUnavailable.MESSAGE
    assert caught.value.__cause__ is None and caught.value.__context__ is None, "the client's error is not attached"
    assert BUCKET not in repr(caught.value)
    assert "tmp/" + OTHER_HEX not in gcs.emulator.objects
    assert gcs.emulator.failures[0].remaining == 0, f"{operation}: the failure was never reached"


def test_a_missing_object_on_delete_is_done(gcs: Bucket) -> None:
    gcs.store.delete_object("tmp/" + HEX)
    assert len(_requests(gcs, "DELETE", "/o/")) == 1, "the store asked, and took 'not found' as done"


def test_reachable_is_a_one_object_listing_and_false_when_the_bucket_does_not_answer(gcs: Bucket) -> None:
    assert gcs.store.reachable() is True
    [query] = _requests(gcs, "GET", f"/b/{BUCKET}/o")
    assert query["maxResults"] == ["1"] and query["prefix"] == ["tmp/"]
    for status in (503, 403, 404):
        gcs.emulator.fail("GET", f"/b/{BUCKET}/o", status=status)
        assert gcs.store.reachable() is False


# ── Keys and listings ────────────────────────────────────────────────────────


@pytest.mark.parametrize("hostile", _hostile_keys(), ids=lambda k: repr(k)[:40])
def test_every_hostile_key_is_refused_before_any_request(gcs: Bucket, hostile: object) -> None:
    attempts: list[Callable[[mo.ObjectStore], object]] = [
        lambda s: s.put_stream(hostile, iter([b"x"]), max_bytes=10),  # type: ignore[arg-type]
        lambda s: s.get_stream(hostile),  # type: ignore[arg-type]
        lambda s: s.stat_object(hostile),  # type: ignore[arg-type]
        lambda s: s.delete_object(hostile),  # type: ignore[arg-type]
    ]
    if hostile is not None:
        attempts.append(lambda s: s.list_objects("tmp/", older_than=FAR_FUTURE, start_after=hostile, limit=3))  # type: ignore[arg-type]
    if hostile != f"assets/{HEX}/":
        attempts.append(lambda s: s.list_objects(hostile, older_than=FAR_FUTURE, limit=3))  # type: ignore[arg-type]
    for attempt in attempts:
        with pytest.raises(mo.InvalidObjectKey) as caught:
            attempt(gcs.store)
        assert str(caught.value) == mo.InvalidObjectKey.MESSAGE
    assert gcs.emulator.requests == [], "a request reached the bucket before the key was checked"


def test_a_listing_never_examines_a_name_outside_the_key_grammar(gcs: Bucket) -> None:
    """Only this service writes the bucket, but a stray name must still never
    be examined, returned or handed back as a cursor a later call would refuse."""
    keys = sorted("tmp/" + f"{n * 7919:032x}" for n in range(1, 5))
    strays = ["tmp/README", "tmp/" + HEX.upper(), "tmp/" + HEX + "/thumb", "tmp/" + HEX[:-1], "tmp/"]
    for name in keys + strays:
        gcs.emulator.put(name, b"k", when=T0 - timedelta(days=3))
    walked: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = gcs.store.list_objects("tmp/", older_than=T0, start_after=cursor, limit=2)
        assert all(mo.check_key(key) == key for key in page.keys)
        assert page.examined == len(page.keys)
        walked.extend(page.keys)
        cursor = page.cursor
        if cursor is None:
            break
        mo.check_key(cursor)
    assert walked == keys


def test_a_listing_asks_for_a_bounded_page_of_names_and_times_only(gcs: Bucket) -> None:
    gcs.store.list_objects("assets/", older_than=FAR_FUTURE, start_after="assets/" + HEX, limit=7)
    [query] = _requests(gcs, "GET", f"/b/{BUCKET}/o")
    assert query["maxResults"] == ["9"], "the limit, the cursor's own key, and one more to know whether more remain"
    assert query["startOffset"] == ["assets/" + HEX] and query["prefix"] == ["assets/"]
    assert query["fields"] == ["items(name,updated),nextPageToken"]


# ── Metadata the store cannot trust ───────────────────────────────────────────


class _Unused:
    """A bucket no test reaches."""


@dataclass
class _Blob:
    name: str | None = None
    size: int | None = None
    generation: int | None = None
    updated: datetime | None = None
    served: bytes = b""

    def download_as_bytes(self, **_: object) -> bytes:
        return self.served


class _StubBucket:
    def __init__(self, found: _Blob) -> None:
        self.found = found

    def get_blob(self, blob_name: str, **_: object) -> _Blob:
        return self.found

    def blob(self, blob_name: str, **_: object) -> _Blob:
        return self.found


@pytest.mark.parametrize("missing", ["size", "generation", "updated"])
def test_metadata_without_a_size_generation_or_time_is_unavailable(missing: str) -> None:
    fields: dict[str, object] = {"size": 3, "generation": 7, "updated": T0}
    fields[missing] = None
    store = media_gcs.CloudStorageObjectStore(_StubBucket(_Blob(**fields)))  # type: ignore[arg-type]
    with pytest.raises(mo.ObjectStoreUnavailable):
        store.stat_object("tmp/" + HEX)


def test_a_range_that_comes_back_short_is_unavailable() -> None:
    store = media_gcs.CloudStorageObjectStore(
        _StubBucket(_Blob(size=10, generation=7, updated=T0, served=b"short"))  # type: ignore[arg-type]
    )
    with pytest.raises(mo.ObjectStoreUnavailable):
        b"".join(store.get_stream("tmp/" + HEX))


# ── The production builder ───────────────────────────────────────────────────


def test_the_builder_uses_the_runtimes_credentials_and_bounded_calls(
    gcs: Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key, no credentials file: the client resolves its own. Pointed at
    the emulator the way Compose would point it at one, it reaches the bucket
    the setting names, with the per-request timeout and the retry deadline."""
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", gcs.emulator.endpoint)
    monkeypatch.delenv(media_gcs.NO_BUCKET_METADATA_READ)
    built = media_gcs.build_gcs_store(BUCKET)
    gcs.emulator.reset_log()
    assert built.reachable() is True
    assert [r.path for r in gcs.emulator.requests] == [f"/storage/v1/b/{BUCKET}/o"]
    assert built._timeout == media_gcs.REQUEST_TIMEOUT
    assert getattr(built._retry, "timeout", None) == media_gcs.RETRY_DEADLINE_S
    assert media_gcs.RETRY_DEADLINE_S < 30.0, "inside a job's advisory budget (job_driver.JOB_BUDGET_S)"


def _bucket_reads(emulator: Emulator) -> list[str]:
    return [r.path for r in emulator.requests if r.method == "GET" and r.path == f"/storage/v1/b/{BUCKET}"]


def _some_calls(store: mo.ObjectStore) -> None:
    store.put_stream("tmp/" + HEX, iter([b"abc"]), max_bytes=10)
    store.stat_object("tmp/" + HEX)
    b"".join(store.get_stream("tmp/" + HEX))
    store.list_objects("tmp/", older_than=FAR_FUTURE, limit=3)
    store.delete_object("tmp/" + HEX)


def test_the_store_asks_for_nothing_but_objects_and_the_builder_keeps_it_so(
    gcs: Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left alone, the client reads the bucket's metadata in a background
    thread on its first call (a `storage.buckets.get` the runtime account does
    not hold). The first half shows the probe sees that read; the second, that
    the builder switches it off, so the store sends only what it asks for."""
    monkeypatch.delenv(media_gcs.NO_BUCKET_METADATA_READ, raising=False)
    client = storage.Client(
        project="media-test", credentials=AnonymousCredentials(), client_options={"api_endpoint": gcs.emulator.endpoint}
    )
    _some_calls(media_gcs.CloudStorageObjectStore(client.bucket(BUCKET), retry=None))
    deadline = time.monotonic() + 10
    while not _bucket_reads(gcs.emulator) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _bucket_reads(gcs.emulator), "the probe never saw the client's own bucket read"

    monkeypatch.setenv("STORAGE_EMULATOR_HOST", gcs.emulator.endpoint)
    gcs.emulator.reset_log()
    built = media_gcs.build_gcs_store(BUCKET)
    assert os.environ[media_gcs.NO_BUCKET_METADATA_READ] == "true"
    _some_calls(built)
    time.sleep(0.5)
    assert _bucket_reads(gcs.emulator) == []
    assert all("/o" in r.path for r in gcs.emulator.requests), "every request names objects"
