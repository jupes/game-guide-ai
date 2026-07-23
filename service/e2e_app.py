"""Deterministic production-route app for browser E2E tests.

The route table is the real service app. Only external boundaries are replaced:
the startup lifespan is empty, chat answers are deterministic, and history uses
the production in-memory store contract.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .app import app, get_message_store, get_service
from .history import InMemoryMessageStore
from .models import ChatMode, ChatResponse


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
app.router.lifespan_context = e2e_lifespan
app.dependency_overrides[get_service] = DeterministicRagService
app.dependency_overrides[get_message_store] = lambda: message_store
