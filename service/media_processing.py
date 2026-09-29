"""Judging and processing uploaded media (agent-forge-harness-1kg.8.1.2, slice b
of the media bead). The upload route is `service/assets_api.py`.

**Judged by its bytes** (SEC-25). `sniff` reads the first `SNIFF_BYTES` of a
body and names PNG, JPEG, WebP, MP3, M4A, OGG or WAV by magic bytes, or nothing.
SVG (a document that carries script), GIF (refused in v1) and everything else
are nothing, and so is a sniffed type that is not the declared one: the route
answers 415 before a byte is kept. A name never takes part: the system never
receives one (SEC-28).

**Nothing uploaded is served as it arrived** (SEC-27, MS-6). `process` turns
the upload into the object that will be served:

* **Images**, in a Python subprocess (`python -m service.media_processing
  image ...`): Pillow reads the header first — each side against
  `IMAGE_MAX_SIDE`, the pixel count against `IMAGE_MAX_PIXELS`, with
  `Image.MAX_IMAGE_PIXELS` at HALF the cap because Pillow only warns there and
  raises at twice it (RV-12) — and only then decodes, restricted to the
  declared family's decoder. The pixels are re-encoded within that family (PNG
  to PNG, JPEG to JPEG, WebP to WebP) from a fresh image that carries no EXIF,
  no ICC profile, no ancillary chunk and no trailer; alpha is kept where the
  family has it, and an EXIF orientation is applied to the pixels before it is
  dropped.
* **Audio**, with `ffprobe` and then `ffmpeg`, each with `-protocol_whitelist
  file,pipe` and the declared family's demuxer named explicitly, so no input
  can make either tool open anything but the one scratch file: exactly one
  audio stream, a video stream refused unless it is an attached picture
  (cover art, RV-13), the duration against `AMBIENCE_MAX_MS` — never a
  one-shot's 30 s, which is the cue route's (`1kg.8.5`, AUDIO-26) — then a
  transcode to MP3 (LAME, VBR about 130 kbps) with every container tag,
  chapter, lyric and attached picture dropped and no ID3 tag written at all,
  the muxer's encoder tag included (RV-28).

**Bounded** (MS-6, SEC-27). One job at a time per instance (`GATE`); a wall
clock of `WALL_CLOCK_S` over the whole job; an address-space limit of
`ADDRESS_SPACE_BYTES` on every child on POSIX, set by the child itself before it
decodes anything (never a `preexec_fn`, which is unsafe in a threaded server);
a scratch directory the caller deletes; and an environment carrying nothing
but `PATH`, so the process that parses hostile bytes holds no credential.
Cloud Run offers no network namespace; the tools are kept off the network by
the protocol whitelist and by Pillow, which never fetches.

A refusal is `ProcessingFailed` carrying an `AssetFailure` — a closed reason and
never a message. A tool that is not installed is `ProcessingUnavailable`, a
fault of the deployment and not of the file. Nothing here logs.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import anyio

from .media_objects import CHUNK_BYTES
from .workbench_contracts import AMBIENCE_MAX_MS, IMAGE_MAX_PIXELS, IMAGE_MAX_SIDE, AssetFailure, AssetKind

#: Enough of a body to tell every accepted family apart (WebP and WAV need 12).
SNIFF_BYTES = 16
#: One job's whole budget, probe and transcode together (*suggested*, MS-6).
WALL_CLOCK_S = 60.0
#: `RLIMIT_AS` for every processing child on POSIX (*suggested*, MS-6).
ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
#: How long an upload waits for its turn before it is told to retry (MS-6).
QUEUE_BOUND_S = 60.0
#: What that answer's `Retry-After` says.
RETRY_AFTER_S = 30
#: How often a waiting upload looks at the gate again.
GATE_POLL_S = 0.05
PROCESSED_AUDIO_TYPE = "audio/mpeg"

#: Pillow's format name for each accepted image type: the only decoder tried.
IMAGE_FORMATS: Mapping[str, str] = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
#: ffmpeg's demuxer for each accepted audio type, named so that no probing of
#: the input can pick another one (a playlist, a concat list, a network URL).
AUDIO_DEMUXERS: Mapping[str, str] = {"audio/mpeg": "mp3", "audio/mp4": "mov", "audio/ogg": "ogg", "audio/wav": "wav"}
_M4A_BRANDS = frozenset({b"M4A ", b"M4B ", b"mp41", b"mp42", b"isom", b"iso2"})
_ROOT = Path(__file__).resolve().parent.parent


class ProcessingFailed(Exception):
    """The file is refused, for a reason from the contract's closed set."""

    def __init__(self, failure: AssetFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class ProcessingUnavailable(Exception):
    """A processing tool is missing or could not be started: the deployment's
    fault, answered as `503 backend_unavailable`, never as the file's."""

    def __init__(self) -> None:
        super().__init__("media processing is unavailable")


# ── Judging the first bytes ──────────────────────────────────────────────────


def sniff(head: bytes) -> str | None:
    """The accepted media type these first bytes begin, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head[4:8] == b"ftyp" and head[8:12] in _M4A_BRANDS:
        return "audio/mp4"
    if head.startswith(b"ID3") or _mpeg_layer_three(head):
        return "audio/mpeg"
    return None


def _mpeg_layer_three(head: bytes) -> bool:
    """An MPEG audio frame header, Layer III, with no reserved field. ADTS
    (raw AAC) has layer bits 00 and is not one; JPEG's FF D8 has no sync."""
    if len(head) < 4 or head[0] != 0xFF or head[1] & 0xE0 != 0xE0:
        return False
    version, layer = (head[1] >> 3) & 0x03, (head[1] >> 1) & 0x03
    bitrate, sampling = head[2] >> 4, (head[2] >> 2) & 0x03
    return version != 0x01 and layer == 0x01 and bitrate != 0x0F and sampling != 0x03


# ── One job at a time ────────────────────────────────────────────────────────


class ProcessingGate:
    """MS-6's semaphore of one for the whole process.

    A thread lock, polled from the event loop rather than awaited: it serves
    every event loop and thread in the process, and an upload waiting its turn
    holds no thread while it waits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def acquire(self, wait_s: float) -> bool:
        deadline = time.monotonic() + wait_s
        while not self._lock.acquire(blocking=False):
            if time.monotonic() >= deadline:
                return False
            await anyio.sleep(GATE_POLL_S)
        return True

    def release(self) -> None:
        self._lock.release()

    def busy(self) -> bool:
        return self._lock.locked()


GATE = ProcessingGate()


# ── Processing ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Processed:
    """What will be served, and what was measured of it."""

    path: Path
    media_type: str
    width: int | None
    height: int | None
    duration_ms: int | None


def process(
    kind: AssetKind, declared: str, source: Path, scratch: Path, *, wall_clock_s: float | None = None
) -> Processed:
    """Turn `source` into what may be served, inside `scratch`, or refuse it."""
    deadline = time.monotonic() + (WALL_CLOCK_S if wall_clock_s is None else wall_clock_s)
    if kind is AssetKind.IMAGE:
        return _process_image(declared, source, scratch, deadline)
    return _process_audio(declared, source, scratch, deadline)


def _process_image(declared: str, source: Path, scratch: Path, deadline: float) -> Processed:
    target = scratch / "out"
    done = _run(image_argv(IMAGE_FORMATS[declared], source, target), deadline)
    report = _report(done.stdout) if done.returncode == 0 else None
    if report is None:
        raise ProcessingFailed(AssetFailure.UNREADABLE)
    failure = report.get("failure")
    if failure is not None:
        known = {reason.value: reason for reason in AssetFailure}
        raise ProcessingFailed(known.get(str(failure), AssetFailure.UNREADABLE))
    width, height = report.get("width"), report.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or not target.is_file():
        raise ProcessingFailed(AssetFailure.UNREADABLE)
    return Processed(target, declared, width, height, None)


def _process_audio(declared: str, source: Path, scratch: Path, deadline: float) -> Processed:
    demuxer = AUDIO_DEMUXERS[declared]
    probed = _run(ffprobe_argv(demuxer, source), deadline)
    if probed.returncode != 0:
        raise ProcessingFailed(AssetFailure.UNREADABLE)
    duration_ms = probe_verdict(probed.stdout)
    target = scratch / "out.mp3"
    done = _run(ffmpeg_argv(demuxer, source, target), deadline)
    if done.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise ProcessingFailed(AssetFailure.UNREADABLE)
    return Processed(target, PROCESSED_AUDIO_TYPE, None, None, duration_ms)


def image_argv(pillow_format: str, source: Path, target: Path) -> list[str]:
    return [sys.executable, "-m", "service.media_processing", "image", pillow_format, str(source), str(target)]


def ffprobe_argv(demuxer: str, source: Path) -> list[str]:
    return [
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", demuxer,
        "-show_entries", "stream=codec_type:stream_disposition=attached_pic:format=duration",
        "-of", "json", str(source),
    ]


def ffmpeg_argv(demuxer: str, source: Path, target: Path) -> list[str]:
    """The one audio stream, to MP3, with nothing of the input's but its sound."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
        "-f", demuxer, "-i", str(source),
        "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1", "-vn", "-sn", "-dn",
        "-c:a", "libmp3lame", "-q:a", "5", "-id3v2_version", "0", "-write_id3v1", "0",
        # No version string in the Info frame either: bitexact leaves ffmpeg's
        # fixed "Lavf" there instead of this build's encoder version.
        "-fflags", "+bitexact", "-flags:a", "+bitexact",
        "-f", "mp3", str(target),
    ]


def probe_verdict(output: bytes) -> int:
    """The duration in milliseconds, if `ffprobe`'s report is of one audio
    stream (plus attached pictures) no longer than `AMBIENCE_MAX_MS`."""
    report = _report(output)
    streams = report.get("streams") if report is not None else None
    found = report.get("format") if report is not None else None
    if not isinstance(streams, list) or not isinstance(found, dict):
        raise ProcessingFailed(AssetFailure.UNREADABLE)
    kinds = [stream.get("codec_type") if isinstance(stream, dict) else None for stream in streams]
    for stream, kind in zip(streams, kinds, strict=True):
        disposition = stream.get("disposition") if isinstance(stream, dict) else None
        if kind == "video" and not (isinstance(disposition, dict) and disposition.get("attached_pic") == 1):
            raise ProcessingFailed(AssetFailure.UNSUPPORTED_TYPE)
    audio = kinds.count("audio")
    if audio != 1:
        raise ProcessingFailed(AssetFailure.UNSUPPORTED_TYPE if audio else AssetFailure.UNREADABLE)
    try:
        seconds = float(found["duration"])
    except (KeyError, TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise ProcessingFailed(AssetFailure.UNREADABLE)
    duration_ms = math.ceil(seconds * 1000)
    if duration_ms > AMBIENCE_MAX_MS:
        raise ProcessingFailed(AssetFailure.TOO_LONG)
    return duration_ms


def file_chunks(path: Path) -> Iterator[bytes]:
    """A file in `CHUNK_BYTES` pieces, for an object store's `put_stream`."""
    with open(path, "rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            yield chunk


# ── Running a child ──────────────────────────────────────────────────────────


def limited(argv: Sequence[str]) -> list[str]:
    """`argv` behind the launcher that limits its address space and then
    becomes it (`exec`), on POSIX; on any other system `argv` itself."""
    if os.name != "posix":
        return list(argv)
    return [sys.executable, "-m", "service.media_processing", "exec", *argv]


def child_env() -> dict[str, str]:
    """`PATH` to find the tools, and on Windows what Python needs to start."""
    kept = ("PATH", "SYSTEMROOT")
    return {name: os.environ[name] for name in kept if name in os.environ}


def _run(argv: Sequence[str], deadline: float) -> subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProcessingFailed(AssetFailure.TIMED_OUT)
    outcome: subprocess.CompletedProcess[bytes] | None = None
    failure: Exception | None = None
    try:
        outcome = subprocess.run(
            argv if argv[0] == sys.executable else limited(argv),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=remaining, cwd=_ROOT, env=child_env(), check=False,
        )
    except subprocess.TimeoutExpired:
        failure = ProcessingFailed(AssetFailure.TIMED_OUT)
    except OSError:
        failure = ProcessingUnavailable()
    # Raised outside the handler: the tool's own error names paths.
    if failure is not None:
        raise failure
    assert outcome is not None
    return outcome


def _report(output: bytes) -> dict[str, object] | None:
    lines = output.decode("utf-8", "replace").strip().splitlines()
    try:
        found = json.loads("\n".join(lines))
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


# ── The children ─────────────────────────────────────────────────────────────


def limit_address_space() -> None:
    """RLIMIT_AS on POSIX, before anything is decoded. Whether Cloud Run's
    sandbox enforces it is unverified (media ADR section 9); `1kg.9.5`
    measures it."""
    if os.name != "posix":
        return
    resource = importlib.import_module("resource")
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))


def configure_pillow() -> int:
    """`Image.MAX_IMAGE_PIXELS` at half the cap (RV-12). Returns the value set."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS // 2
    return Image.MAX_IMAGE_PIXELS


def image_child(pillow_format: str, source: str, target: str) -> dict[str, object]:
    """Header first, then decode, then a fresh image of the pixels alone."""
    import warnings

    from PIL import Image, ImageOps

    configure_pillow()
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    try:
        with Image.open(source, formats=[pillow_format]) as opened:
            width, height = opened.size
            if width > IMAGE_MAX_SIDE or height > IMAGE_MAX_SIDE or width * height > IMAGE_MAX_PIXELS:
                return {"failure": AssetFailure.TOO_MANY_PIXELS.value}
            opened.load()
            upright = ImageOps.exif_transpose(opened)
            alpha = upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info
            grey = upright.mode in ("1", "L")
            if pillow_format == "JPEG":
                mode = "L" if grey else "RGB"
            else:
                mode = "RGBA" if alpha else ("L" if grey else "RGB")
            pixels = upright.convert(mode)
            clean = Image.frombytes(mode, pixels.size, pixels.tobytes())
            clean.save(target, pillow_format, **({} if pillow_format == "PNG" else {"quality": 90}))
            return {"width": clean.width, "height": clean.height}
    except Image.DecompressionBombError:
        return {"failure": AssetFailure.TOO_MANY_PIXELS.value}
    except (OSError, ValueError, SyntaxError, EOFError, MemoryError):
        return {"failure": AssetFailure.UNREADABLE.value}


def main(argv: Sequence[str]) -> int:  # pragma: no cover - runs only as a child process
    """`exec <tool> ...` becomes the tool under the limit; `image <format>
    <source> <target>` is the image child. Each limits itself first."""
    if len(argv) >= 2 and argv[0] == "exec":
        limit_address_space()
        os.execvp(argv[1], list(argv[1:]))
    if len(argv) == 4 and argv[0] == "image":
        limit_address_space()
        sys.stdout.write(json.dumps(image_child(argv[1], argv[2], argv[3])) + "\n")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
