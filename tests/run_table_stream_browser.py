"""Run the opt-in two-service player table profile against local PostgreSQL."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import urlopen

from _pg import DSN, connect, throwaway_database
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from test_reveal_db import _campaign, _document, _parts, _pg_world, _seat, _session, _write

from service import migrations
from service.reveal_scope import TableTarget
from service.session import SessionData, encode_session

ROOT = Path(__file__).resolve().parent.parent
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SERVE = """
import asyncio
import socket
import sys
from pathlib import Path

import uvicorn
from service.app import app

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    Path(sys.argv[1]).write_text(str(listener.getsockname()[1]), encoding="ascii")
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    asyncio.run(server.serve(sockets=[listener]), loop_factory=asyncio.SelectorEventLoop)
"""


def _require_local_database() -> None:
    if DSN is None or not DSN.startswith(("postgresql://", "postgres://")):
        raise SystemExit("Set DATABASE_URL to the URI of your local PostgreSQL test server.")
    try:
        settings = conninfo_to_dict(DSN)
    except ProgrammingError:
        raise SystemExit("DATABASE_URL is not a valid local PostgreSQL URI.") from None
    if settings.get("host") not in LOOPBACK or settings.get("hostaddr", "127.0.0.1") not in LOOPBACK:
        raise SystemExit("The table browser profile requires a loopback PostgreSQL test server.")


@contextmanager
def _service(env: dict[str, str], address: Path) -> Iterator[str]:
    with subprocess.Popen(
        [sys.executable, "-c", _SERVE, str(address)],
        cwd=ROOT,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and process.poll() is None:
                if address.is_file() and (port := address.read_text(encoding="ascii").strip()).isdigit():
                    origin = f"http://127.0.0.1:{port}"
                    try:
                        with urlopen(f"{origin}/healthz", timeout=0.5) as reply:
                            if reply.status == 200:
                                break
                    except (URLError, OSError):
                        pass
                time.sleep(0.1)
            else:
                raise RuntimeError("The local table service did not reach readiness.")
            yield origin
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


def _fixture(dsn: str, media_dir: Path) -> dict[str, str]:
    migrations.migrate(dsn)
    world = _pg_world(dsn, fill=True)
    campaign = _campaign(world)
    session = _session(world, campaign)
    player = world.players[0]
    seat = _seat(world, campaign, "confirmed", player)
    pending = world.players[1]
    _seat(world, campaign, "accepted", pending)
    offered = world.players[2]
    _seat(world, campaign, "offered", offered)
    outsider = world.players[3]
    shared, private = _document(world, campaign), _document(world, campaign)
    _write(world, campaign, session.id, shared, TableTarget())
    _write(world, campaign, session.id, private, _parts(seat), mask=("qualifier",))
    with world.db.transaction() as unit:
        _, grant = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
        _, exit_grant = world.sessions.mint_screen(unit, campaign, session.id, owner_id=world.owner)
    with connect(dsn) as connection:
        connection.execute("UPDATE auth.users SET role = 'dm' WHERE id IN (%s, %s)", (world.owner, world.other_owner))

    secret = secrets.token_urlsafe(48)
    return os.environ | {
        "DATABASE_URL": dsn,
        "MIGRATIONS_MODE": "verify",
        "WORKBENCH_TABLE_STREAMS_ENABLED": "1",
        "WORKBENCH_MEDIA_ENABLED": "1",
        "WORKBENCH_MEDIA_STORE": "filesystem",
        "WORKBENCH_MEDIA_DIR": str(media_dir),
        "DB_ASYNC_POOL_MAX": "2",
        "SESSION_SECRET": secret,
        "SESSION_COOKIE_NAME": "gga_session",
        "SESSION_COOKIE_SECURE": "0",
        "RAG_TRACING": "0",
        "OPENAI_API_KEY": "local-table-browser-profile",
        "TABLE_BROWSER_CAMPAIGN": campaign,
        "TABLE_BROWSER_SESSION": session.id,
        "TABLE_BROWSER_PUBLIC_DOCUMENT": shared,
        "TABLE_BROWSER_OWNER_COOKIE": encode_session(SessionData(world.owner, "dm"), secret),
        "TABLE_BROWSER_OTHER_GM_COOKIE": encode_session(SessionData(world.other_owner, "dm"), secret),
        "TABLE_BROWSER_PLAYER_COOKIE": encode_session(SessionData(player, "player"), secret),
        "TABLE_BROWSER_PENDING_COOKIE": encode_session(SessionData(pending, "player"), secret),
        "TABLE_BROWSER_OFFERED_COOKIE": encode_session(SessionData(offered, "player"), secret),
        "TABLE_BROWSER_OUTSIDER_COOKIE": encode_session(SessionData(outsider, "player"), secret),
        "TABLE_BROWSER_SCREEN_COOKIE": grant,
        "TABLE_BROWSER_EXIT_SCREEN_COOKIE": exit_grant,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify live player tables and private GM media across two local services."
    )
    parser.add_argument("--browser", choices=("chromium", "webkit"), default="chromium")
    args = parser.parse_args()
    _require_local_database()
    bun = shutil.which("bun")
    if bun is None or not (ROOT / "ui/dist/table.html").is_file():
        raise SystemExit("Build the mobile frontend in ui/ with bun run build before running the profile.")
    with throwaway_database("table_browser") as dsn, TemporaryDirectory(prefix="table-browser-") as scratch:
        root = Path(scratch)
        env = _fixture(dsn, root / "media")
        with _service(env, root / "player-port") as player, _service(env, root / "screen-port") as screen:
            subprocess.run(
                [bun, "run", "test:e2e", "--config", "playwright.table-stream.config.ts"],
                cwd=ROOT / "ui",
                check=True,
                timeout=80,
                env=env
                | {
                    "TABLE_BROWSER_ENGINE": args.browser,
                    "TABLE_BROWSER_PLAYER_URL": player,
                    "TABLE_BROWSER_SCREEN_URL": screen,
                },
            )


if __name__ == "__main__":
    main()
