"""The deterministic E2E service must start without DB, LLM, or Langfuse."""

import importlib
import sys

from fastapi.testclient import TestClient

import service.app as production


def test_e2e_startup_is_isolated_and_persists_chat_history(monkeypatch):
    class UnexpectedProductionDependency:
        def __init__(self, *args, **kwargs):
            raise AssertionError("E2E startup constructed a production dependency")

    monkeypatch.setattr(production, "RagService", UnexpectedProductionDependency)
    monkeypatch.setattr(
        production,
        "PostgresMessageStore",
        UnexpectedProductionDependency,
    )
    original_lifespan = production.app.router.lifespan_context
    original_overrides = dict(production.app.dependency_overrides)
    try:
        e2e = importlib.import_module("service.e2e_app")
        with TestClient(e2e.app) as client:
            health = client.get("/healthz")
            response = client.post(
                "/chat",
                json={
                    "prompt": "How does magic missile work?",
                    "mode": "spell",
                    "conversation_id": "conversation-1",
                },
            )
            history = client.get("/conversations/conversation-1/messages")
    finally:
        production.app.router.lifespan_context = original_lifespan
        production.app.dependency_overrides.clear()
        production.app.dependency_overrides.update(original_overrides)
        sys.modules.pop("service.e2e_app", None)

    assert health.json()["status"] == "ok"
    assert response.status_code == 200
    assert response.json()["answer"] == (
        "E2E spell answer: How does magic missile work?"
    )
    assert [message["role"] for message in history.json()["messages"]] == [
        "user",
        "assistant",
    ]
