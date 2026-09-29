"""The object store, its keys and its settings (agent-forge-harness-1kg.8.1.1).

Slice a of the media bead: no route, no database. What is asserted here:

* **The contract** every object store keeps (L-11(a), AC-4), run over the
  in-memory store and the filesystem store alike: a round trip, reads by offset
  and length, the ceiling that ends a stream the moment it is passed and leaves
  nothing behind, bounded chunks, an idempotent delete, and the ordered, paged
  listing whose cursor always moves on.
* **The keys** (L-11(b), AC-5): one grammar, checked before any I/O; minted
  from 16 CSPRNG bytes and from nothing else.
* **The filesystem store's own guards**: a key maps to one path under the root,
  an original and its derivatives coexist, a symlink cannot lead out of the
  root, and an operating-system error is the named store-unavailable error.
* **The settings and the factory** (L-12, AC-6): off by default, `memory` never
  selectable from the environment, `gcs` refused until slice d.
* **The chokepoint** (L-11(d), AC-13): every object-store call outside the
  object-store module goes through `via_store`.

    uv run --frozen --no-sync python -m pytest service/tests/test_media_objects.py -q
"""

from __future__ import annotations

import ast
import builtins
import os
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from service import asset_store
from service import media_objects as mo

SERVICE = Path(__file__).resolve().parents[1]
MIGRATIONS = SERVICE / "sql" / "migrations"
#: Found by its name, never by its number: the lead renumbers at merge (R-7).
[MEDIA_SQL_PATH] = sorted(MIGRATIONS.glob("*_media_assets.sql"))

HEX = "0123456789abcdef" * 2
OTHER_HEX = "fedcba9876543210" * 2
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _hex(n: int) -> str:
    return f"{n:032x}"


# ── The stores, and how a test ages an object in each ─────────────────────────


@dataclass
class Store:
    kind: str
    store: mo.ObjectStore
    #: Makes the object at `key` look last written at `when`.
    age: Callable[[str, datetime], None]
    root: Path | None


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(params=["memory", "filesystem"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    if request.param == "memory":
        clock = _Clock()
        memory = mo.InMemoryObjectStore(clock=clock)

        def age_memory(key: str, when: datetime) -> None:
            data, _ = memory._objects[key]
            memory._objects[key] = (data, when)

        return Store("memory", memory, age_memory, None)
    root = tmp_path / "media"
    root.mkdir()
    disk = mo.FilesystemObjectStore(root)

    def age_disk(key: str, when: datetime) -> None:
        stamp = when.timestamp()
        os.utime(disk._path(key), (stamp, stamp))

    return Store("filesystem", disk, age_disk, root)


def _put(store: mo.ObjectStore, key: str, data: bytes, *, max_bytes: int = 10_000_000) -> int:
    return store.put_stream(key, iter([data]), max_bytes=max_bytes)


def _read(store: mo.ObjectStore, key: str, **window: int) -> bytes:
    return b"".join(store.get_stream(key, **window))


FAR_FUTURE = T0 + timedelta(days=36500)


# ── AC-4: the contract, over both stores ─────────────────────────────────────


def test_a_round_trip_returns_the_same_bytes_and_counts_them(store: Store) -> None:
    body = bytes(range(256)) * 5000
    chunks = [body[i : i + 70_000] for i in range(0, len(body), 70_000)]
    written = store.store.put_stream("assets/" + HEX, iter(chunks), max_bytes=len(body))
    assert written == len(body)
    assert _read(store.store, "assets/" + HEX) == body
    stat = store.store.stat_object("assets/" + HEX)
    assert stat is not None and stat.size == len(body)


@pytest.mark.parametrize(
    ("offset", "length", "expected"),
    [
        pytest.param(0, 0, b"", id="zero-length"),
        pytest.param(9, 1, b"9", id="the-last-byte"),
        pytest.param(9, 5, b"9", id="a-length-past-the-end"),
        pytest.param(10, 1, b"", id="an-offset-at-the-end"),
        pytest.param(25, 3, b"", id="an-offset-past-the-end"),
        pytest.param(2, 3, b"234", id="inside"),
    ],
)
def test_a_read_honours_its_offset_and_length(store: Store, offset: int, length: int, expected: bytes) -> None:
    _put(store.store, "tmp/" + HEX, b"0123456789")
    assert _read(store.store, "tmp/" + HEX, offset=offset, length=length) == expected
    assert _read(store.store, "tmp/" + HEX, offset=offset) == b"0123456789"[offset:]


@pytest.mark.parametrize("window", [{"offset": -1}, {"length": -1}, {"offset": True}])
def test_a_read_refuses_a_window_that_is_not_one(store: Store, window: dict[str, int]) -> None:
    _put(store.store, "tmp/" + HEX, b"abc")
    with pytest.raises(ValueError, match="offset|length"):
        store.store.get_stream("tmp/" + HEX, **window)


def test_an_endless_stream_ends_in_the_ceiling_error_and_leaves_nothing(store: Store) -> None:
    """The store stops pulling the moment the count passes the ceiling, and
    publishes nothing: no key, and on disk no temporary file either."""
    pulled = 0

    def endless() -> Iterator[bytes]:
        nonlocal pulled
        while True:
            pulled += 1
            yield b"x" * 1000

    with pytest.raises(mo.ObjectTooLarge):
        store.store.put_stream("tmp/" + HEX, endless(), max_bytes=10_000)
    assert pulled <= 10_000 // 1000 + 1, "the store read past the ceiling plus one chunk"
    assert store.store.stat_object("tmp/" + HEX) is None
    if store.root is not None:
        assert [p for p in store.root.rglob("*") if p.is_file()] == [], "a temporary file survived"


def test_a_body_of_exactly_the_ceiling_is_accepted(store: Store) -> None:
    assert _put(store.store, "tmp/" + HEX, b"y" * 4096, max_bytes=4096) == 4096


def test_the_ceiling_is_a_whole_number_of_bytes(store: Store) -> None:
    for ceiling in (0, -5, True):
        with pytest.raises(ValueError, match="ceiling"):
            store.store.put_stream("tmp/" + HEX, iter([b"a"]), max_bytes=ceiling)


def test_every_chunk_a_read_yields_is_within_the_bound(store: Store) -> None:
    """MS-7's 256 KiB: a slow phone holds a slot, not the file."""
    body = b"z" * (3 * mo.CHUNK_BYTES + 5)
    _put(store.store, "assets/" + HEX, body, max_bytes=len(body))
    sizes = [len(chunk) for chunk in store.store.get_stream("assets/" + HEX)]
    assert sum(sizes) == len(body)
    assert len(sizes) >= 4 and max(sizes) <= mo.CHUNK_BYTES


def test_a_missing_object_is_the_named_error(store: Store) -> None:
    with pytest.raises(mo.ObjectMissing):
        store.store.get_stream("assets/" + HEX)
    assert store.store.stat_object("assets/" + HEX) is None


def test_delete_is_idempotent(store: Store) -> None:
    _put(store.store, "tmp/" + HEX, b"bytes")
    store.store.delete_object("tmp/" + HEX)
    store.store.delete_object("tmp/" + HEX)
    store.store.delete_object("assets/" + OTHER_HEX)
    assert store.store.stat_object("tmp/" + HEX) is None


def test_an_original_and_its_derivatives_coexist_and_delete_independently(store: Store) -> None:
    """A filesystem cannot hold `assets/<hex>` as a file and `assets/<hex>/thumb`
    below it, so the store maps keys to paths one-to-one; this is that map."""
    original, thumb, peaks = "assets/" + HEX, f"assets/{HEX}/thumb", f"assets/{HEX}/peaks-v1"
    for key, data in ((original, b"original"), (thumb, b"thumb"), (peaks, b"peaks"), ("tmp/" + HEX, b"tmp")):
        _put(store.store, key, data)
    assert [_read(store.store, k) for k in (original, thumb, peaks, "tmp/" + HEX)] == [
        b"original", b"thumb", b"peaks", b"tmp",
    ]
    listed = store.store.list_objects("assets/", older_than=FAR_FUTURE, limit=10)
    assert listed.keys == (original, peaks, thumb)
    under = store.store.list_objects(f"assets/{HEX}/", older_than=FAR_FUTURE, limit=10)
    assert under.keys == (peaks, thumb), "a derivative listing holds the derivatives only"

    store.store.delete_object(thumb)
    assert store.store.stat_object(original) is not None and store.store.stat_object(peaks) is not None
    store.store.delete_object(original)
    assert _read(store.store, peaks) == b"peaks"
    store.store.delete_object(peaks)
    assert store.store.list_objects("assets/", older_than=FAR_FUTURE, limit=10).keys == ()
    assert _read(store.store, "tmp/" + HEX) == b"tmp"


# ── AC-4: the ordered, paged listing ─────────────────────────────────────────


def _seed_listing(store: Store, count: int, prefix: str = "tmp/") -> list[str]:
    keys = [prefix + _hex(n * 7919) for n in range(1, count + 1)]
    for key in keys:
        _put(store.store, key, b"k")
    return sorted(keys)


def test_a_listing_filters_by_prefix_and_returns_keys_in_byte_order(store: Store) -> None:
    tmp = _seed_listing(store, 5, "tmp/")
    assets = _seed_listing(store, 3, "assets/")
    assert store.store.list_objects("tmp/", older_than=FAR_FUTURE, limit=50).keys == tuple(tmp)
    assert store.store.list_objects("assets/", older_than=FAR_FUTURE, limit=50).keys == tuple(assets)
    assert list(tmp) == sorted(tmp, key=lambda k: k.encode("ascii"))


def test_a_listing_filters_by_age(store: Store) -> None:
    keys = _seed_listing(store, 4)
    for key in keys[:2]:
        store.age(key, T0 - timedelta(days=3))
    for key in keys[2:]:
        store.age(key, T0)
    page = store.store.list_objects("tmp/", older_than=T0 - timedelta(days=1), limit=50)
    assert page.keys == tuple(keys[:2])
    assert page.examined == 4 and page.cursor is None


def test_a_listing_resumes_strictly_after_its_start(store: Store) -> None:
    keys = _seed_listing(store, 6)
    page = store.store.list_objects("tmp/", older_than=FAR_FUTURE, start_after=keys[2], limit=50)
    assert page.keys == tuple(keys[3:])
    between = keys[2][:-1] + chr(ord(keys[2][-1]) + 1) if keys[2][-1] != "f" else keys[2]
    if between not in keys:
        assert store.store.list_objects(
            "tmp/", older_than=FAR_FUTURE, start_after=between, limit=50
        ).keys == tuple(k for k in keys if k > between)


def test_a_listing_examines_at_most_its_limit_even_when_nothing_matches(store: Store) -> None:
    """The bound counts objects examined, not matches, so a page of young
    objects still moves the cursor on (L-11(a))."""
    keys = _seed_listing(store, 7)
    page = store.store.list_objects("tmp/", older_than=T0 - timedelta(days=30), limit=3)
    assert page.keys == ()
    assert page.examined == 3
    assert page.cursor == page.last == keys[2], "the cursor is the last key examined"
    following = store.store.list_objects("tmp/", older_than=T0 - timedelta(days=30), start_after=page.cursor, limit=3)
    assert following.cursor == keys[5]


def test_a_listing_says_none_once_it_is_exhausted(store: Store) -> None:
    keys = _seed_listing(store, 4)
    exhausted = store.store.list_objects("tmp/", older_than=FAR_FUTURE, limit=4)
    assert (exhausted.cursor, exhausted.last) == (None, keys[-1])
    assert store.store.list_objects("tmp/", older_than=FAR_FUTURE, limit=3).cursor == keys[2]
    last = store.store.list_objects("tmp/", older_than=FAR_FUTURE, start_after=keys[-1], limit=3)
    assert (last.keys, last.examined, last.last, last.cursor) == ((), 0, None, None)
    assert store.store.list_objects("assets/", older_than=FAR_FUTURE, limit=3).cursor is None


def test_walking_the_pages_from_the_start_visits_every_key_exactly_once(store: Store) -> None:
    keys = _seed_listing(store, 11)
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(20):
        page = store.store.list_objects("tmp/", older_than=FAR_FUTURE, start_after=cursor, limit=3)
        seen.extend(page.keys)
        cursor = page.cursor
        if cursor is None:
            break
    assert seen == keys


@pytest.mark.parametrize("limit", [0, -1, True])
def test_a_listing_limit_is_a_positive_whole_number(store: Store, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.store.list_objects("tmp/", older_than=FAR_FUTURE, limit=limit)


def test_a_listing_refuses_a_naive_moment(store: Store) -> None:
    with pytest.raises(ValueError, match="timezone"):
        store.store.list_objects("tmp/", older_than=datetime(2026, 1, 1), limit=3)


def test_reachable_answers(store: Store, tmp_path: Path) -> None:
    assert store.store.reachable() is True
    assert mo.FilesystemObjectStore(tmp_path / "never-made").reachable() is False


# ── AC-4: hostile keys are refused before any filesystem call ────────────────


def _hostile_keys() -> list[object]:
    """Built in code: a typed NUL is how a source file once became binary."""
    return [
        "",
        "..",
        "tmp/..",
        f"tmp/../{HEX}",
        f"assets/{HEX}/../../etc",
        f"tmp\\{HEX}",
        f"assets\\{HEX}",
        f"tmp/{HEX}{chr(0)}",
        f"tmp/{HEX[:-1]}{chr(0)}",
        f"assets%2f{HEX}",
        f"assets%2F{HEX}",
        f"/tmp/{HEX}",
        f"C:\\tmp\\{HEX}",
        f"tmp/{HEX.upper()}",
        f"other/{HEX}",
        f"tmp/{HEX}/thumb",
        f"assets/{HEX}/",
        f"assets/{HEX}/Thumb",
        f"assets/{HEX}/_thumb",
        f"assets/{HEX}/" + "t" * 33,
        f"tmp/{HEX[:-1]}",
        f"tmp/{HEX}0",
        f"tmp/{HEX}\n",
        "tmp/" + "\uff10" * 32,
        f"assets/{HEX}/thumb/more",
        b"tmp/" + HEX.encode(),
        None,
        7,
    ]


FS_CALLS = (
    "open", "stat", "lstat", "scandir", "listdir", "makedirs", "mkdir", "replace",
    "unlink", "remove", "rmdir", "utime", "access", "fdopen",
)


def _recording_every_filesystem_call(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def recorder(name: str) -> Callable[..., object]:
        def record(*args: object, **kwargs: object) -> object:
            calls.append(name)
            raise AssertionError(f"{name} was called for a hostile key")

        return record

    for name in FS_CALLS:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, recorder(f"os.{name}"))
    monkeypatch.setattr(os.path, "realpath", recorder("os.path.realpath"))
    monkeypatch.setattr(os.path, "isdir", recorder("os.path.isdir"))
    monkeypatch.setattr(builtins, "open", recorder("open"))
    return calls


@pytest.mark.parametrize("hostile", _hostile_keys(), ids=lambda k: repr(k)[:40])
def test_every_hostile_key_is_refused_before_any_filesystem_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile: object
) -> None:
    disk = mo.FilesystemObjectStore(tmp_path)
    memory = mo.InMemoryObjectStore(clock=_Clock())
    attempts: list[Callable[[mo.ObjectStore], object]] = [
        lambda s: s.put_stream(hostile, iter([b"x"]), max_bytes=10),  # type: ignore[arg-type]
        lambda s: s.get_stream(hostile),  # type: ignore[arg-type]
        lambda s: s.stat_object(hostile),  # type: ignore[arg-type]
        lambda s: s.delete_object(hostile),  # type: ignore[arg-type]
    ]
    if hostile is not None:  # no cursor at all is how a listing starts
        attempts.append(
            lambda s: s.list_objects("tmp/", older_than=FAR_FUTURE, start_after=hostile, limit=3)  # type: ignore[arg-type]
        )
    if hostile != f"assets/{HEX}/":  # not a key, but exactly a derivative listing's prefix
        attempts.append(lambda s: s.list_objects(hostile, older_than=FAR_FUTURE, limit=3))  # type: ignore[arg-type]
    with monkeypatch.context() as patch:
        calls = _recording_every_filesystem_call(patch)
        refused: list[str] = []
        for attempt in attempts:
            for target in (disk, memory):
                with pytest.raises(mo.InvalidObjectKey) as caught:
                    attempt(target)
                refused.append(str(caught.value))
    assert calls == [], "a filesystem call happened before the key was checked"
    assert set(refused) == {mo.InvalidObjectKey.MESSAGE}, "every refusal is the one fixed message"
    if isinstance(hostile, str) and hostile:
        assert all(hostile not in message for message in refused)


def test_the_grammar_admits_exactly_the_three_key_shapes() -> None:
    assert mo.check_key("tmp/" + HEX) == "tmp/" + HEX
    assert mo.check_key("assets/" + HEX) == "assets/" + HEX
    assert mo.check_key(f"assets/{HEX}/thumb_2-x") == f"assets/{HEX}/thumb_2-x"
    assert mo.check_prefix("tmp/") == "tmp/"
    assert mo.check_prefix("assets/") == "assets/"
    assert mo.check_prefix(f"assets/{HEX}/") == f"assets/{HEX}/"
    for prefix in ("", "tmp", "assets", f"tmp/{HEX}/", f"assets/{HEX}", "../", f"assets/{HEX.upper()}/"):
        with pytest.raises(mo.InvalidObjectKey):
            mo.check_prefix(prefix)
    assert mo.parent_key(f"assets/{HEX}/thumb") == "assets/" + HEX
    assert mo.parent_key("assets/" + HEX) == "assets/" + HEX
    assert mo.parent_key("tmp/" + HEX) == "tmp/" + HEX


# ── The filesystem store's own guards ────────────────────────────────────────


def test_the_filesystem_map_is_one_to_one_and_stays_under_the_root(tmp_path: Path) -> None:
    disk = mo.FilesystemObjectStore(tmp_path)
    keys = ["tmp/" + HEX, "assets/" + HEX, f"assets/{HEX}/thumb", f"assets/{HEX}/peaks", "assets/" + OTHER_HEX]
    paths = [disk._path(key) for key in keys]
    assert len(set(paths)) == len(keys)
    root = tmp_path.resolve()
    for path in paths:
        assert Path(os.path.realpath(path)).is_relative_to(root)
    assert not any(str(a).startswith(str(b) + os.sep) for a in paths for b in paths if a != b), (
        "no key's file may be a directory another key needs"
    )


def test_a_symlink_that_leads_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, root / "assets", target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            pytest.skip("this Windows account may not create symlinks; CI runs Linux, where this runs")
        raise
    disk = mo.FilesystemObjectStore(root)
    with pytest.raises(mo.PathEscapesStore):
        _put(disk, "assets/" + HEX, b"secret")
    with pytest.raises(mo.PathEscapesStore):
        disk.stat_object("assets/" + HEX)
    assert list(outside.iterdir()) == [], "nothing was written outside the root"
    assert issubclass(mo.PathEscapesStore, mo.ObjectStoreUnavailable)
    # Every other operation is contained too. A file that happens to sit where
    # the escaped path leads is never read, listed or unlinked through the link.
    planted = outside / f"{HEX}.o"
    planted.write_bytes(b"not the store's")
    (outside / f"{HEX}.d").mkdir()
    (outside / f"{HEX}.d" / "thumb").write_bytes(b"nor this")
    for key in ("assets/" + HEX, f"assets/{HEX}/thumb"):
        with pytest.raises(mo.PathEscapesStore):
            disk.delete_object(key)
        with pytest.raises(mo.PathEscapesStore):
            b"".join(disk.get_stream(key))
    # The last prefix names nothing that exists out there, so only the
    # listing's own check on the directory it reads can refuse it.
    for prefix in ("assets/", f"assets/{HEX}/", f"assets/{OTHER_HEX}/"):
        with pytest.raises(mo.PathEscapesStore):
            disk.list_objects(prefix, older_than=FAR_FUTURE, limit=10)
    assert planted.read_bytes() == b"not the store's"
    assert (outside / f"{HEX}.d" / "thumb").read_bytes() == b"nor this"


def test_an_operating_system_error_is_the_named_store_unavailable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = mo.FilesystemObjectStore(tmp_path)
    _put(disk, "tmp/" + OTHER_HEX, b"kept")

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "denied", str(tmp_path / "tmp" / HEX))

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", refuse)
        with pytest.raises(mo.ObjectStoreUnavailable) as caught:
            _put(disk, "tmp/" + HEX, b"lost")
    assert str(caught.value) == mo.ObjectStoreUnavailable.MESSAGE
    assert caught.value.__context__ is None, "the driver's error, which names a path, is not attached"
    assert disk.stat_object("tmp/" + HEX) is None
    assert sorted(p.name for p in (tmp_path / "tmp").iterdir()) == [f"{OTHER_HEX}.o"], "no temporary file"

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", refuse)
        with pytest.raises(mo.ObjectStoreUnavailable):
            disk.stat_object("tmp/" + OTHER_HEX)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", refuse)
        with pytest.raises(mo.ObjectStoreUnavailable):
            disk.delete_object("tmp/" + OTHER_HEX)
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", refuse)
        with pytest.raises(mo.ObjectStoreUnavailable):
            disk.list_objects("tmp/", older_than=FAR_FUTURE, limit=3)


def test_a_stream_that_fails_part_way_leaves_nothing_on_disk(tmp_path: Path) -> None:
    disk = mo.FilesystemObjectStore(tmp_path)

    def broken() -> Iterator[bytes]:
        yield b"first"
        raise ConnectionResetError("the client went away")

    with pytest.raises(mo.ObjectStoreUnavailable):
        disk.put_stream("tmp/" + HEX, broken(), max_bytes=100)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


# ── AC-5: keys are random and say nothing ────────────────────────────────────


def test_ten_thousand_minted_keys_are_unique_and_grammatical() -> None:
    objects = [asset_store.new_object_key() for _ in range(10_000)]
    temps = [asset_store.new_tmp_key() for _ in range(10_000)]
    assert len(set(objects)) == 10_000 and len(set(temps)) == 10_000
    assert all(re.fullmatch(mo.OBJECT_KEY_PATTERN, key) for key in objects)
    assert all(re.fullmatch(mo.TMP_KEY_PATTERN, key) for key in temps)
    assert all(mo.check_key(key) == key for key in objects + temps)
    assert not {k.removeprefix("assets/") for k in objects} & {k.removeprefix("tmp/") for k in temps}


def test_a_key_is_a_function_of_the_random_source_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the random source fixed, whatever else differs — the asset, the
    campaign, the alt text — the key does not: nothing else reaches it."""
    draws: list[int] = []

    def fixed(nbytes: int) -> str:
        draws.append(nbytes)
        return HEX

    monkeypatch.setattr(asset_store.secrets, "token_hex", fixed)
    minted = {
        (asset_store.new_object_key(), asset_store.new_tmp_key())
        for _asset, _campaign, _alt in (
            ("ast_" + "a" * 22, "cmp_" + "a" * 22, "A red door"),
            ("ast_" + "b" * 22, "cmp_" + "b" * 22, "The villain's portrait"),
        )
    }
    assert minted == {("assets/" + HEX, "tmp/" + HEX)}
    assert draws == [16] * 4, "16 CSPRNG bytes, one draw per key"


def test_the_two_keys_of_an_asset_are_drawn_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter([HEX, OTHER_HEX])
    monkeypatch.setattr(asset_store.secrets, "token_hex", lambda nbytes: next(values))
    assert (asset_store.new_object_key(), asset_store.new_tmp_key()) == ("assets/" + HEX, "tmp/" + OTHER_HEX)


def test_the_minting_functions_read_nothing_but_their_random_source() -> None:
    tree = ast.parse(Path(asset_store.__file__).read_text(encoding="utf-8"))
    minting = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"new_object_key", "new_tmp_key"}
    }
    assert set(minting) == {"new_object_key", "new_tmp_key"}
    for name, function in minting.items():
        arguments = function.args
        assert not (arguments.args or arguments.kwonlyargs or arguments.vararg or arguments.kwarg), name
        body = [node for statement in function.body for node in ast.walk(statement)]
        loaded = {node.id for node in body if isinstance(node, ast.Name)}
        assert loaded <= {"secrets", "KEY_BYTES"}, f"{name} reads {sorted(loaded)}"
        attributes = {node.attr for node in body if isinstance(node, ast.Attribute)}
        assert attributes == {"token_hex"}, name


def test_the_migration_checks_the_keys_with_the_grammar_this_module_enforces() -> None:
    body = "\n".join(
        line for line in MEDIA_SQL_PATH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    assert f"CHECK (object_key ~ '{mo.OBJECT_KEY_PATTERN}')" in body
    assert f"CHECK (tmp_key ~ '{mo.TMP_KEY_PATTERN}')" in body
    assert mo.OBJECT_KEY_PATTERN == r"^assets/[0-9a-f]{32}$"
    assert mo.TMP_KEY_PATTERN == r"^tmp/[0-9a-f]{32}$"
    assert (asset_store.OBJECT_KEY_PATTERN, asset_store.TMP_KEY_PATTERN) == (
        mo.OBJECT_KEY_PATTERN, mo.TMP_KEY_PATTERN,
    ), "the asset store spells the grammar too, because it imports no object store"


# ── AC-6: settings, and the factory ──────────────────────────────────────────


def test_the_defaults_are_off_and_no_store() -> None:
    settings = mo.MediaSettings.from_env({})
    assert settings == mo.MediaSettings(enabled=False, store=None, media_dir=None)
    assert mo.build_object_store(settings) is None
    assert mo.MediaSettings.from_env({"WORKBENCH_MEDIA_ENABLED": "", "WORKBENCH_MEDIA_STORE": ""}).store is None


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)])
def test_the_switch_reads_its_documented_values(raw: str, expected: bool) -> None:
    assert mo.MediaSettings.from_env({"WORKBENCH_MEDIA_ENABLED": raw}).enabled is expected


@pytest.mark.parametrize("raw", ["yes", "TRUE", "on", "2", " true", "enabled"])
def test_the_switch_refuses_anything_else_by_name(raw: str) -> None:
    with pytest.raises(mo.MediaSettingsError, match="WORKBENCH_MEDIA_ENABLED") as caught:
        mo.MediaSettings.from_env({"WORKBENCH_MEDIA_ENABLED": raw})
    assert str(caught.value) == "WORKBENCH_MEDIA_ENABLED must be one of: true, false, 1, 0", (
        "one fixed sentence, whatever the value was"
    )


def test_memory_cannot_be_selected_from_the_environment() -> None:
    with pytest.raises(mo.MediaSettingsError, match="WORKBENCH_MEDIA_STORE") as caught:
        mo.MediaSettings.from_env({"WORKBENCH_MEDIA_STORE": "memory"})
    assert "memory" not in str(caught.value)
    built = mo.build_object_store(mo.MediaSettings(store="memory"))
    assert isinstance(built, mo.InMemoryObjectStore), "in code, for tests and the E2E app, it is"


@pytest.mark.parametrize("directory", [None, "", "media/uploads", "./media"])
def test_the_filesystem_store_needs_an_absolute_directory(directory: str | None) -> None:
    env = {"WORKBENCH_MEDIA_STORE": "filesystem"}
    if directory is not None:
        env["WORKBENCH_MEDIA_DIR"] = directory
    with pytest.raises(mo.MediaSettingsError, match="WORKBENCH_MEDIA_DIR") as caught:
        mo.MediaSettings.from_env(env)
    if directory:
        assert directory not in str(caught.value)


def test_the_filesystem_store_is_built_from_an_absolute_directory(tmp_path: Path) -> None:
    settings = mo.MediaSettings.from_env({"WORKBENCH_MEDIA_STORE": "filesystem", "WORKBENCH_MEDIA_DIR": str(tmp_path)})
    assert settings == mo.MediaSettings(enabled=False, store="filesystem", media_dir=tmp_path)
    built = mo.build_object_store(settings)
    assert isinstance(built, mo.FilesystemObjectStore) and built.reachable()


def test_gcs_is_refused_by_a_named_error_and_imports_no_client() -> None:
    """Vacuous until slice d: `google-cloud-storage` is not installed, so the
    `sys.modules` half proves nothing today. It is kept as the regression
    guard for the day slice d adds the library — that day it is the proof that
    choosing no store, or `filesystem`, never imports the client (AC 11(iii))."""
    with pytest.raises(mo.MediaStoreNotBuilt) as caught:
        mo.MediaSettings.from_env({"WORKBENCH_MEDIA_STORE": "gcs"})
    assert "gcs" not in str(caught.value)
    with pytest.raises(mo.MediaStoreNotBuilt):
        mo.build_object_store(mo.MediaSettings(store="gcs"))
    mo.build_object_store(mo.MediaSettings.from_env({}))
    assert "google.cloud.storage" not in sys.modules


@pytest.mark.parametrize("raw", ["s3", "Filesystem", "disk", "gcs "])
def test_an_unknown_store_is_refused_by_name_without_its_value(raw: str) -> None:
    with pytest.raises(mo.MediaSettingsError, match="WORKBENCH_MEDIA_STORE") as caught:
        mo.MediaSettings.from_env({"WORKBENCH_MEDIA_STORE": raw})
    assert str(caught.value) == "WORKBENCH_MEDIA_STORE must be unset or name a store this build provides"
    assert not isinstance(caught.value, mo.MediaStoreNotBuilt), "only `gcs` itself is slice d's"


def test_the_settings_are_read_from_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKBENCH_MEDIA_ENABLED", raising=False)
    monkeypatch.delenv("WORKBENCH_MEDIA_STORE", raising=False)
    assert mo.MediaSettings.from_env() == mo.MediaSettings()
    monkeypatch.setenv("WORKBENCH_MEDIA_ENABLED", "true")
    assert mo.MediaSettings.from_env().enabled is True


def test_nothing_in_the_running_service_reads_these_settings_yet() -> None:
    """L-12: slice b wires them. Until then no module but this one names them."""
    naming = [
        path.name
        for path in SERVICE.glob("*.py")
        if path.name != "media_objects.py" and "WORKBENCH_MEDIA_" in path.read_text(encoding="utf-8")
    ]
    assert naming == []


# ── AC-13: one chokepoint for every object-store call ────────────────────────


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _through_the_chokepoint(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether `node` sits inside a lambda that is the second argument of a
    `via_store(...)` call."""
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(current, ast.Lambda) and isinstance(parent, ast.Call):
            called = parent.func
            name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
            if name == "via_store" and len(parent.args) >= 2 and parent.args[1] is current:
                return True
        current = parent
    return False


def _store_calls(source: str, name: str) -> list[tuple[str, bool]]:
    """Every object-store method call in `source`, in line order: where it is,
    and whether it goes through the chokepoint."""
    tree = ast.parse(source)
    parents = _parents(tree)
    return sorted(
        (f"{name}:{node.lineno}", _through_the_chokepoint(node, parents))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in mo.STORE_METHODS
    )


def _service_store_calls() -> list[tuple[str, bool]]:
    return [
        call
        for path in sorted(SERVICE.glob("*.py"))
        if path.name != "media_objects.py"
        for call in _store_calls(path.read_text(encoding="utf-8"), path.name)
    ]


def test_every_object_store_call_outside_its_module_goes_through_the_chokepoint() -> None:
    """Requirement 2.5: slice c puts the thread limiter inside `via_store`, so a
    call that goes round it would be a call the limiter never sees."""
    calls = _service_store_calls()
    assert [where for where, through in calls if not through] == []
    assert any(where.startswith("asset_jobs.py") for where, _ in calls), (
        "no call was found at all, so this check proved nothing"
    )


def test_the_chokepoint_check_sees_a_direct_call_and_only_a_call_inside_via_store_passes() -> None:
    """So the check above cannot pass by finding nothing: this shows what it
    sees, beside the job handlers' own calls that the check above requires."""
    source = (
        "def direct(store):\n    store.delete_object('tmp/x')\n"
        "def through(store):\n    via_store(store, lambda s: s.delete_object('tmp/x'))\n"
        "def the_lambda_first(store):\n    via_store(lambda s: s.delete_object('tmp/x'), store)\n"
        "def a_lambda_elsewhere(store):\n    run(store, lambda s: s.list_objects('tmp/'))\n"
    )
    assert _store_calls(source, "sample") == [
        ("sample:2", False), ("sample:4", True), ("sample:6", False), ("sample:8", False)
    ]


def test_the_chokepoint_hands_the_call_to_the_store_it_was_given() -> None:
    memory = mo.InMemoryObjectStore(clock=_Clock())
    assert mo.via_store(memory, lambda s: s.stat_object("tmp/" + HEX)) is None
    assert mo.via_store(memory, lambda s: s.reachable()) is True
    memory.put_stream("tmp/" + OTHER_HEX, iter([b"abc"]), max_bytes=10)
    found = mo.via_store(memory, lambda s: s.stat_object("tmp/" + OTHER_HEX))
    assert found is not None and found.size == 3, "the call reached the store it was given, not another one"
