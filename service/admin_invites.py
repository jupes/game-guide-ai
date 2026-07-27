"""
Admin invite CLI (x5bz.2 Checkpoint B).

The pilot admin (the operator, with DB access via the Cloud SQL proxy) mints,
lists, and revokes one-time invite links from the command line — no in-app admin
surface to secure. The invite carries the role, and the printed link is
``<base-url>/?invite=<token>`` (root path, since the SPA has no client router and
StaticFiles 404s on other paths).

    python -m service.admin_invites create --role dm --base-url https://<svc>.run.app
    python -m service.admin_invites list
    python -m service.admin_invites revoke <token>

The `cmd_*` functions take a store so they test against the in-memory fake;
`main()` wires the real PostgresAuthStore.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

import config

from .auth_store import AuthStore, PostgresAuthStore
from .invites import ROLES, Invite, Role


def build_signup_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/?invite={token}"


def cmd_create(store: AuthStore, role: Role, ttl_days: int, base_url: str) -> str:
    # A zero/negative TTL mints an already-expired invite while still printing a
    # perfectly ordinary-looking link — the operator only finds out when the
    # tester can't sign up. Reject it up front.
    if ttl_days < 1:
        raise ValueError(f"--ttl-days must be at least 1 (got {ttl_days})")
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    invite = store.create_invite(role=role, expires_at=expires_at)
    return build_signup_link(base_url, invite.token)


def _status(invite: Invite, now: datetime) -> str:
    if invite.is_revoked:
        return "revoked"
    if invite.is_used:
        return "used"
    if invite.expires_at <= now:
        return "expired"
    return "open"


def format_invites(invites: list[Invite], now: datetime | None = None) -> str:
    """Full tokens, deliberately: `revoke` takes a whole token, so a truncated
    listing would leave an operator unable to act on what they can see."""
    now = now or datetime.now(timezone.utc)
    if not invites:
        return "(no invites)"
    lines = [f"{'ROLE':<6} {'STATUS':<8} {'EXPIRES':<26} TOKEN"]
    for inv in invites:
        lines.append(
            f"{inv.role:<6} {_status(inv, now):<8} {inv.expires_at.isoformat():<26} {inv.token}"
        )
    return "\n".join(lines)


def cmd_list(store: AuthStore) -> str:
    return format_invites(store.list_invites())


def cmd_revoke(store: AuthStore, token: str) -> bool:
    return store.revoke_invite(token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m service.admin_invites")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="mint a one-time invite link")
    p_create.add_argument("--role", choices=list(ROLES), default="player")
    p_create.add_argument("--ttl-days", type=int, default=config.INVITE_TTL_DAYS)
    p_create.add_argument(
        "--base-url", default=os.environ.get("APP_BASE_URL", "http://localhost:8000"),
    )
    sub.add_parser("list", help="list all invites")
    p_revoke = sub.add_parser("revoke", help="revoke an unused invite")
    p_revoke.add_argument("token")

    args = parser.parse_args(argv)
    store = PostgresAuthStore()
    store.ensure_schema()

    if args.cmd == "create":
        try:
            print(cmd_create(store, args.role, args.ttl_days, args.base_url))
        except ValueError as exc:
            parser.error(str(exc))
        return 0
    if args.cmd == "list":
        print(cmd_list(store))
        return 0
    if args.cmd == "revoke":
        ok = cmd_revoke(store, args.token)
        print("revoked" if ok else "not revocable (unknown, already used, or revoked)")
        return 0 if ok else 1
    return 2  # pragma: no cover - argparse requires a subcommand


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
