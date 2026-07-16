# BRIEFING — 2026-07-14T20:32:50+08:00

## Mission
Investigate the LINE Bot project in /home/pipadmin/文件/line_bot to find why users receive the AI service unavailable error.

## 🔒 My Identity
- Archetype: explorer
- Roles: Read-only investigator
- Working directory: /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_1
- Original parent: 8a89e8e2-0763-4b1c-b351-6f9021cbf9a3
- Milestone: Diagnostic Investigation

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Operation mode: CODE_ONLY network mode (No external HTTP/network access)
- Language: 繁體中文（台灣）

## Current Parent
- Conversation ID: 8a89e8e2-0763-4b1c-b351-6f9021cbf9a3
- Updated: 2026-07-14T20:32:50+08:00

## Investigation State
- **Explored paths**:
  - `app.py`, `semantic_cache.py`, `sync_embeddings.py`, `supabase_schema.sql`, `.env`
  - `/home/pipadmin/文件/flask.log`, `/home/pipadmin/文件/llama.log`
  - Systemd user service configurations (`linebot-flask.service`, `linebot-llama.service`)
  - Local listening ports with `ss -tlnp`
- **Key findings**:
  - **Context Overflow Error (400 Bad Request):** Llama-server has `--ctx-size 4096 --parallel 2`, allocating only `2048` tokens context window per parallel slot. Prompt sent by bot is `2876` tokens, exceeding the slot limit and causing the LLM request to fail.
  - **Gemini API Error (404 Not Found):** `v1beta` endpoint version is requested for `text-embedding-004` model in `semantic_cache.py` and `sync_embeddings.py`. This model name is not supported on `/v1beta/` but is supported on `/v1/`.
  - **FastAPI / Supabase:** The local FastAPI app is running on port 5000, and it successfully connects to local Supabase on port 8000.
- **Unexplored areas**: None (All diagnostics completed)

## Key Decisions Made
- Reported context overflow and embedding endpoint issues as the root causes of the LINE Bot failure.

## Artifact Index
- `analysis.md` — Detailed analysis report on findings and fixes.
- `handoff.md` — Handoff report for subsequent implementation steps.
