# Project: LINE Bot AI Reply Fix

## Architecture
- LINE Bot built with FastAPI (`app.py`), connected to:
  - Local LLM service on port 8080 (base_url: `http://127.0.0.1:8080/v1`) using OpenAI API client (AsyncOpenAI).
  - Supabase client for multi-tenant and session management.
- Faulty state: Bot replies "抱歉，AI 服務暫時無法使用，請稍後再試。" to users when receiving messages.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| 1 | Diagnostics & RCA | Investigate runtime logs, test connectivity to local LLM and Supabase, and identify the root cause. | None | DONE |
| 2 | Implement Fix | Implement code modifications or service startup scripts to resolve the connection/timeout/database issues. | M1 | DONE |
| 3 | Verification | Simulate LINE Webhook POST request to verify the bot processes messages correctly and returns 200 OK. | M2 | DONE |

## Interface Contracts
- FastAPI app endpoint: `POST /callback` or similar webhook endpoint.
- Local LLM service: HTTP `POST http://127.0.0.1:8080/v1/chat/completions` returning OpenAI-compatible JSON.

## Code Layout
- `app.py`: Main FastAPI application logic.
- `semantic_cache.py`: Local semantic cache utility.
- `sync_embeddings.py`: Embeddings synchronization script.
- `supabase_schema.sql`: SQL schema definition.
- `.env`: Environment variables configuration.
