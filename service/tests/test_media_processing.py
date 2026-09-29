"""Judging and processing uploaded media (agent-forge-harness-1kg.8.1.2).

The pure rules — the magic bytes, `ffprobe`'s verdict, the tools' arguments,
the gate, the limits — run everywhere. What needs `ffmpeg` is
`service/tests/test_assets_api.py`'s, through the route, on the served bytes.

Run from the repo root:
    uv run python -m pytest service/tests/test_media_processing.py -q
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from service import media_processing as mp
from service.workbench_contracts import AMBIENCE_MAX_MS, IMAGE_MAX_PIXELS, MEDIA_TYPES, AssetFailure, AssetKind

# justification: `Any` here types ffprobe's JSON report and a monkeypatched
# `subprocess.run`'s pass-through arguments; no production value is `Any`.

# ── Fixtures, built in code ──────────────────────────────────────────────────


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def forged_png(width: int, height: int) -> bytes:
    """A PNG whose header claims `width` x `height` and whose pixel data is
    garbage: a decode would fail, so only a header check can refuse it by size."""
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", b"\x00garbage" * 8)
            + png_chunk(b"IEND", b""))


def encoded(fmt: str, size: tuple[int, int] = (24, 16), mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(out, fmt)
    return out.getvalue()


HEADS: dict[str, bytes] = {
    "image/png": encoded("PNG"),
    "image/jpeg": encoded("JPEG"),
    "image/webp": encoded("WEBP"),
    "audio/mpeg": b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 16,
    "audio/ogg": b"OggS\x00\x02" + b"\x00" * 20,
    "audio/wav": b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 12,
    "audio/mp4": b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00" + b"\x00" * 12,
}

REFUSED: dict[str, bytes] = {
    "gif": b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
    "svg": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    "html": b"<!doctype html><html><script>alert(1)</script></html>",
    "zip": b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 16,
    "adts aac": b"\xff\xf1\x50\x80\x02\x1f\xfc" + b"\x00" * 16,
    "empty": b"",
    "video mp4 brand": b"\x00\x00\x00\x20ftypqt  \x00\x00\x02\x00" + b"\x00" * 12,
}


# ── SEC-25: judged by the bytes ──────────────────────────────────────────────


def test_every_accepted_type_is_recognised_by_its_magic_bytes() -> None:
    assert set(HEADS) == {media for kind in AssetKind for media in MEDIA_TYPES[kind]}
    assert {declared: mp.sniff(head[: mp.SNIFF_BYTES]) for declared, head in HEADS.items()} == {
        declared: declared for declared in HEADS
    }


def test_a_bare_mpeg_layer_three_frame_is_mp3_and_its_near_misses_are_not() -> None:
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 12  # MPEG-1 Layer III, 128 kbps, 44.1 kHz
    assert mp.sniff(frame) == "audio/mpeg"
    near = {
        "layer I": b"\xff\xff\x90\x64", "reserved version": b"\xff\xeb\x90\x64",
        "bad bitrate": b"\xff\xfb\xf0\x64", "reserved rate": b"\xff\xfb\x9c\x64", "no sync": b"\xff\x1b\x90\x64",
    }
    assert {name: mp.sniff(head + b"\x00" * 12) for name, head in near.items()} == dict.fromkeys(near)


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_svg_gif_html_archives_raw_aac_and_nothing_are_not_accepted(name: str) -> None:
    assert mp.sniff(REFUSED[name][: mp.SNIFF_BYTES]) is None


# ── The audio probe's verdict (MS-6, RV-13) ──────────────────────────────────


def report(*streams: dict[str, Any], duration: object = "2.000000") -> bytes:
    return json.dumps({"streams": list(streams), "format": {"duration": duration}}).encode()


AUDIO = {"codec_type": "audio"}
COVER = {"codec_type": "video", "disposition": {"attached_pic": 1}}
VIDEO = {"codec_type": "video", "disposition": {"attached_pic": 0}}


def verdict(output: bytes) -> int | AssetFailure:
    try:
        return mp.probe_verdict(output)
    except mp.ProcessingFailed as failed:
        return failed.failure


def test_the_probe_accepts_one_audio_stream_and_cover_art_and_measures_it() -> None:
    assert verdict(report(AUDIO)) == 2000
    assert verdict(report(AUDIO, COVER)) == 2000, "cover art is what -vn drops"
    assert verdict(report(AUDIO, {"codec_type": "data"})) == 2000, "a data stream is dropped by -map 0:a:0"
    assert verdict(report(AUDIO, duration="0.0004")) == 1, "rounded up, never to zero"


def test_the_probe_refuses_video_a_second_audio_stream_nothing_and_garbage() -> None:
    assert verdict(report(AUDIO, VIDEO)) is AssetFailure.UNSUPPORTED_TYPE
    assert verdict(report(AUDIO, {"codec_type": "video"})) is AssetFailure.UNSUPPORTED_TYPE
    assert verdict(report(AUDIO, AUDIO)) is AssetFailure.UNSUPPORTED_TYPE
    assert verdict(report()) is AssetFailure.UNREADABLE
    assert verdict(report(AUDIO, duration="N/A")) is AssetFailure.UNREADABLE
    assert verdict(report(AUDIO, duration=None)) is AssetFailure.UNREADABLE
    assert verdict(report(AUDIO, duration="inf")) is AssetFailure.UNREADABLE
    assert verdict(b"not json") is AssetFailure.UNREADABLE
    assert verdict(b"[]") is AssetFailure.UNREADABLE


def test_the_duration_cap_is_ambience_and_never_a_one_shots() -> None:
    """Requirement 4.5: an upload has no cue kind; `1kg.8.5` checks CUE_MAX_MS."""
    assert verdict(report(AUDIO, duration="40.0")) == 40_000
    assert verdict(report(AUDIO, duration=str(AMBIENCE_MAX_MS / 1000))) == AMBIENCE_MAX_MS
    assert verdict(report(AUDIO, duration="600.001")) is AssetFailure.TOO_LONG
    assert verdict(report(AUDIO, duration="601")) is AssetFailure.TOO_LONG


# ── SEC-27: the tools are told exactly what they may do ─────────────────────


def test_the_tools_read_one_file_through_one_named_demuxer_and_write_nothing_else(tmp_path: Path) -> None:
    source, target = tmp_path / "in", tmp_path / "out.mp3"
    for declared, demuxer in mp.AUDIO_DEMUXERS.items():
        for argv in (mp.ffprobe_argv(demuxer, source), mp.ffmpeg_argv(demuxer, source, target)):
            assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe", declared
            assert argv[argv.index("-f") + 1] == demuxer
            assert argv.index("-f") < argv.index(str(source)), "the demuxer is named for the input"
            assert not any("http" in part or "://" in part for part in argv)
    transcode = mp.ffmpeg_argv("mp3", source, target)
    pairs = {transcode[i]: transcode[i + 1] for i in range(len(transcode) - 1)}
    assert pairs["-map"] == "0:a:0" and pairs["-map_metadata"] == "-1" and pairs["-map_chapters"] == "-1"
    assert pairs["-id3v2_version"] == "0" and pairs["-write_id3v1"] == "0"
    assert pairs["-c:a"] == "libmp3lame" and pairs["-q:a"] == "5"
    assert {"-nostdin", "-vn", "-sn", "-dn"} <= set(transcode)
    assert transcode[-1] == str(target)


def test_a_child_is_given_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    assert set(mp.child_env()) <= {"PATH", "SYSTEMROOT"}
    assert "PATH" in mp.child_env()


def test_the_launcher_limits_the_address_space_on_posix_and_is_absent_elsewhere() -> None:
    argv = ["ffmpeg", "-version"]
    if os.name != "posix":
        assert mp.limited(argv) == argv
        return
    assert mp.limited(argv) == [sys.executable, "-m", "service.media_processing", "exec", *argv]


@pytest.mark.skipif(os.name != "posix" and not os.environ.get("CI"), reason="RLIMIT_AS is POSIX; CI runs Linux")
def test_a_child_really_runs_under_the_address_space_limit() -> None:
    """The POSIX branch, asserted on Linux in CI rather than skipped everywhere."""
    probe = [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"]
    done = subprocess.run(mp.limited(probe), capture_output=True, cwd=Path(mp.__file__).parents[1], check=True,
                          env=mp.child_env(), timeout=60)
    assert int(done.stdout) == mp.ADDRESS_SPACE_BYTES == 512 * 1024 * 1024


def test_the_wall_clock_is_sixty_seconds_and_a_child_past_it_is_timed_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert mp.WALL_CLOCK_S == 60.0
    seen: list[float] = []
    real = subprocess.run

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["timeout"])
        return real(*args, **kwargs)

    source = tmp_path / "in"
    source.write_bytes(encoded("PNG"))
    monkeypatch.setattr(mp.subprocess, "run", recording)
    mp.process(AssetKind.IMAGE, "image/png", source, tmp_path)
    assert len(seen) == 1 and 59 < seen[0] <= 60
    with pytest.raises(mp.ProcessingFailed) as late:
        mp.process(AssetKind.IMAGE, "image/png", source, tmp_path, wall_clock_s=0.001)
    assert late.value.failure is AssetFailure.TIMED_OUT


def test_a_missing_tool_is_the_deployments_fault_not_the_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(mp.subprocess, "run", missing)
    with pytest.raises(mp.ProcessingUnavailable):
        mp.process(AssetKind.AUDIO, "audio/mpeg", tmp_path / "in", tmp_path)


# ── MS-6: one job at a time ──────────────────────────────────────────────────


def test_the_gate_admits_one_job_and_tells_the_next_to_wait_at_most_its_bound() -> None:
    gate = mp.ProcessingGate()

    async def scenario() -> list[bool]:
        first = await gate.acquire(0)
        second = await gate.acquire(0.1)
        gate.release()
        third = await gate.acquire(0)
        busy = gate.busy()
        gate.release()
        return [first, second, third, busy, gate.busy()]

    assert asyncio.run(scenario()) == [True, False, True, True, False]
    assert mp.QUEUE_BOUND_S == 60.0 and isinstance(mp.GATE, mp.ProcessingGate)


def test_a_waiting_job_gets_its_turn_when_the_holder_releases() -> None:
    gate = mp.ProcessingGate()

    async def scenario() -> bool:
        assert await gate.acquire(0)
        waiter = asyncio.ensure_future(gate.acquire(5))
        await asyncio.sleep(0.2)
        assert not waiter.done(), "one at a time"
        gate.release()
        return await waiter

    assert asyncio.run(scenario()) is True


# ── MS-6, SEC-26: images, header first ───────────────────────────────────────


def run_image(tmp_path: Path, data: bytes, declared: str = "image/png") -> mp.Processed | AssetFailure:
    source = tmp_path / "in"
    source.write_bytes(data)
    try:
        return mp.process(AssetKind.IMAGE, declared, source, tmp_path)
    except mp.ProcessingFailed as failed:
        return failed.failure


def test_pillow_is_set_to_half_the_pixel_cap() -> None:
    assert mp.configure_pillow() == IMAGE_MAX_PIXELS // 2 == Image.MAX_IMAGE_PIXELS


def test_the_pixel_and_side_caps_refuse_from_the_header_before_any_decode(tmp_path: Path) -> None:
    """Garbage pixel data: a decode would answer `unreadable`, so only a header
    check can answer `too_many_pixels`."""
    assert run_image(tmp_path, forged_png(8000, 5000)) is AssetFailure.TOO_MANY_PIXELS  # 40 MP
    assert run_image(tmp_path, forged_png(9000, 10)) is AssetFailure.TOO_MANY_PIXELS  # a side past 8,192
    assert run_image(tmp_path, forged_png(40, 40)) is AssetFailure.UNREADABLE  # small, so it is decoded


def test_an_image_is_re_encoded_in_its_family_and_measured(tmp_path: Path) -> None:
    for declared, fmt in mp.IMAGE_FORMATS.items():
        made = run_image(tmp_path, encoded(fmt, (30, 20)), declared)
        assert isinstance(made, mp.Processed)
        assert (made.media_type, made.width, made.height, made.duration_ms) == (declared, 30, 20, None)
        with Image.open(made.path) as out:
            assert out.format == fmt


def test_a_jpeg_under_a_png_label_is_not_decoded_as_anything_else(tmp_path: Path) -> None:
    assert run_image(tmp_path, encoded("JPEG"), "image/png") is AssetFailure.UNREADABLE


def test_file_chunks_never_hands_more_than_one_chunk(tmp_path: Path) -> None:
    source = tmp_path / "big"
    source.write_bytes(b"x" * (mp.CHUNK_BYTES * 2 + 5))
    assert [len(chunk) for chunk in mp.file_chunks(source)] == [mp.CHUNK_BYTES, mp.CHUNK_BYTES, 5]
