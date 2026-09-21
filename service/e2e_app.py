"""Deterministic production-route app for browser E2E tests.

The route table is the real service app — including the real auth guards, so
the E2E exercises the actual signup → session → chat journey. Only external
boundaries are replaced: the startup lifespan is empty, chat answers are
deterministic, and history + auth use the in-memory store contracts.

The auth store mints an invite the first time it is asked for a token starting
with `E2E_INVITE_PREFIX`, because a browser test cannot guess a randomly minted
one. `SESSION_SECRET` and `SESSION_COOKIE_SECURE=0` come from
docker-compose.e2e.yml — the E2E stack is served over plain http, so a Secure
cookie would never come back.
"""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

from .app import app, get_auth_store, get_message_store, get_service
from .auth_store import InMemoryAuthStore, User
from .history import InMemoryMessageStore
from .invites import Invite
from .models import ChatMode, ChatResponse

# Kept in sync with ui/e2e/fixtures.ts and ui/e2e/app.spec.ts, which build their
# tokens from this prefix.
E2E_INVITE_PREFIX = "e2e-invite-"


class E2EInviteStore(InMemoryAuthStore):
    """Mints an E2E invite the first time it is asked for one.

    The store used to be pre-seeded from two fixed lists — `range(5)` retries
    and `range(5)` Playwright workers x 2 slots — and both ceilings were real
    bugs rather than generous margins:

    * Playwright starts a NEW worker process after every failure, so a CI run
      with a handful of genuine failures walked past the last seeded
      `(worker, slot)` token and went red at account creation instead of at the
      assertion that actually broke — the wrong test, with the wrong message.
    * A seeded token is single-use, so a stack served exactly ONE run. The
      documented `E2E_BASE_URL=... bun run test:e2e` workflow failed on the
      second invocation with a 400 from the fixture, which reads like a product
      fault and is not one.

    Minting on demand removes both ceilings without weakening anything the
    suite asserts:

    * Single use is untouched. `redeem_invite` marks the invite `used_at` and
      LEAVES it in the store, so a spent token is found, not re-minted, and
      still fails `check_redeemable`.
    * A token outside the prefix is still unknown, which is what
      `ui/e2e/auth.spec.ts` pins: an invite deep-link carrying
      `not-a-real-invite` must say "Unknown invite link.".
    """

    def _mint_on_first_sight(self, token: str) -> None:
        if token.startswith(E2E_INVITE_PREFIX) and token not in self._invites:
            # DM role so every channel (incl. GM) is reachable in the browser test.
            self.seed_invite(token, role="dm")

    def get_invite(self, token: str) -> Invite | None:
        self._mint_on_first_sight(token)
        return super().get_invite(token)

    def redeem_invite(
        self, token: str, email: str, password_hash: str, now: datetime | None = None,
    ) -> User:
        # /auth/signup pre-checks with get_invite, so this is belt and braces —
        # but it keeps the two entry points telling the same story.
        self._mint_on_first_sight(token)
        return super().redeem_invite(token, email, password_hash, now)


class DeterministicRagService:
    def answer(
        self,
        prompt: str,
        mode: str = "sage",
        conversation_id: str | None = None,
        attachment_context: str | None = None,
        attachment_label: str | None = None,
    ) -> ChatResponse:
        return ChatResponse(
            answer=f"E2E {mode} answer: {prompt}",
            sources=[],
            answerable=True,
            mode=ChatMode(mode),
            conversation_id=conversation_id,
        )


@asynccontextmanager
async def e2e_lifespan(application: FastAPI):
    yield


message_store = InMemoryMessageStore()
auth_store = E2EInviteStore()

app.router.lifespan_context = e2e_lifespan
app.dependency_overrides[get_service] = DeterministicRagService
app.dependency_overrides[get_message_store] = lambda: message_store
app.dependency_overrides[get_auth_store] = lambda: auth_store
