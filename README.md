# rag-chat

A simple RAG (Retrieval-Augmented Generation) chat application composed of three services:

- **Chat App** — User-facing interface for submitting prompts and reading responses
- **Agent Service** — Middle layer that orchestrates embedding lookup and LLM response generation
- **Vector DB** — Stores and serves document embeddings for semantic search

## Architecture

```
User
  ↓ prompt
Chat App
  ↓ query
Agent Service ──→ Embedding Model
  ↓                    ↓ embedding vector
  └──────────→ Vector DB (similarity search)
  ↓ retrieved context + prompt
LLM
  ↓ response
Chat App
```

## Services

| Service | Description |
|---------|-------------|
| `chat-app/` | Frontend chat UI |
| `agent-service/` | REST/WebSocket API — embedding + retrieval + LLM orchestration |
| `vector-db/` | Vector database setup and configuration |
| `ingestion/` | Scripts for chunking and loading documents into the vector DB |

## Getting Started

See `plan.md` for the phased implementation plan.
