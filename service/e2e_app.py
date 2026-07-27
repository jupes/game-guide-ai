"""Deterministic production-route app for browser E2E tests.

The route table is the real service app — including the real auth guards, so
the E2E exercises the actual signup → session → chat journey. Only external
boundaries are replaced: the startup lifespan is empty, chat answers are
deterministic, and history + auth use the in-memory store contracts.

The auth store is seeded with a KNOWN invite token (`E2E_INVITE_TOKEN`) because
a browser test cannot guess a randomly minted one. `SESSION_SECRET` and
`SESSION_COOKIE_SECURE=0` come from docker-compose.e2e.yml — the E2E stack is
served over plain http, so a Secure cookie would never come back.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .app import app, get_auth_store, get_message_store, get_service
from .auth_store import InMemoryAuthStore
from .history import InMemoryMessageStore
from .models import ChatMode, ChatResponse

# Kept in sync with ui/e2e/app.spec.ts.
E2E_INVITE_TOKEN = "e2e-invite-token"


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
auth_store = InMemoryAuthStore()
# DM role so every channel (incl. GM) is reachable in the browser test.
auth_store.seed_invite(E2E_INVITE_TOKEN, role="dm")

app.router.lifespan_context = e2e_lifespan
app.dependency_overrides[get_service] = DeterministicRagService
app.dependency_overrides[get_message_store] = lambda: message_store
app.dependency_overrides[get_auth_store] = lambda: auth_store
