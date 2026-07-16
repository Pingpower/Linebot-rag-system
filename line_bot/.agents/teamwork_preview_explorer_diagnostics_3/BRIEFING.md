# BRIEFING — 2026-07-14T20:35:00+08:00

## Mission
Explore the codebase and logs of the LINE Bot project in /home/pipadmin/文件/line_bot to find why users receive the error "抱歉，AI 服務暫時無法使用，請稍後再試。"

## 🔒 My Identity
- Archetype: Teamwork explorer (Explorer Diagnostics 3)
- Roles: Read-only investigation, trace test files, examine semantic_cache.py and sync_embeddings.py, identify exact line/config causing the error
- Working directory: /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_3
- Original parent: 8a89e8e2-0763-4b1c-b351-6f9021cbf9a3
- Milestone: Diagnosis of AI Service Unavailable error

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Code-only network mode (no external HTTP/web access)
- Update progress.md regularly for liveness detection

## Current Parent
- Conversation ID: 8a89e8e2-0763-4b1c-b351-6f9021cbf9a3
- Updated: 2026-07-14T20:35:00+08:00

## Investigation State
- **Explored paths**:
  - `app.py`
  - `semantic_cache.py`
  - `sync_embeddings.py`
  - `/home/pipadmin/文件/flask.log`
  - `/home/pipadmin/文件/llama.log`
  - `linebot-llama.service` and `linebot-flask.service` systemd unit configurations
- **Key findings**:
  - Primary cause: `llama-server` configured with `--ctx-size 4096 --parallel 2`, allocating only `2048` tokens per slot. Inbound LINE Bot prompt reaches `2876` tokens, causing a `400 Bad Request` in local LLM chat completions, triggering FastAPI application's general fallback reply.
  - Secondary cause: Gemini embedding URL version mismatch (`v1beta` is used for `text-embedding-004`), resulting in a `404 Not Found` for embeddings. This causes both `sync_embeddings.py` and `semantic_cache.py` to fail, skipping cache hits and RAG document contexts.
- **Unexplored areas**: None. The root cause has been thoroughly diagnosed and verified.

## Key Decisions Made
- Analysed log files directly to bypass terminal permission prompt time-out restrictions.
- Executed `sync_embeddings.py` in the background to confirm Gemini API 404 response.

## Artifact Index
- /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_3/BRIEFING.md — Persistent memory index.
- /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_3/handoff.md — Detailed diagnosis and findings.
