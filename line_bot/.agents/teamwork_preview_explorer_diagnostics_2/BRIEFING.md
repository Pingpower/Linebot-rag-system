# BRIEFING — 2026-07-14T20:35:00+08:00

## Mission
探索 LINE Bot 專案的 codebase 與 logs，找出使用者收到「抱歉，AI 服務暫時無法使用，請稍後再試。」錯誤的原因。

## 🔒 My Identity
- Archetype: Explorer
- Roles: Explorer Diagnostics 2
- Working directory: /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_2
- Original parent: 8a89e8e2-0763-4b1c-b351-6f9021cbf9a3
- Milestone: M1 (Diagnostics & RCA)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- CODE_ONLY mode (no external network access, no curl/wget to external URLs)
- Cannot use write_to_file / edit tools outside of the own agent folder (except reports/analysis/metadata within own folder)

## Current Parent
- Conversation ID: 8a89e8e2-0763-4b1c-b351-6f9021cbf9a3
- Updated: 2026-07-14T20:35:00+08:00

## Investigation State
- **Explored paths**:
  - `app.py`
  - `supabase_schema.sql`
  - `sync_embeddings.py`
  - `semantic_cache.py`
  - `.env`
  - `flask.log`
  - `llama.log`
  - `linebot-flask.service`
  - `linebot-llama.service`
- **Key findings**:
  - **Context size limit exceeded (HTTP 400):** 本機 llama-server 參數為 `--ctx-size 4096 --parallel 2`，導致單一並行 slot 的 context 限制為 2048。`app.py` 在組合 style_instruction, assets, 與歷史紀錄時生成了高達 2876 詞標的 prompt，API 因而回傳 400 Bad Request。
  - **Gemini Embedding API 404:** 在 `sync_embeddings.py` 與 `semantic_cache.py` 中，Embedding 請求使用了 `v1beta` 路徑，然而 `text-embedding-004` 在此版本下不可用，必須修改為 `v1` 路徑。
  - **Supabase 與 local LLM 連線正常:** 兩者連線均可成功連通（在 context limit 內時）。
- **Unexplored areas**:
  - 無，調查工作已完成。

## Key Decisions Made
- 建立 `test_diagnostics.py` 用於診斷 Supabase 及本機 LLM 連線。
- 從 `flask.log` 提取精確的異常日誌，順利還原並分析了 400 Bad Request 以及 404 Embedding 錯誤的發生成因。

## Artifact Index
- /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_2/analysis.md — Detailed analysis report
- /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_2/handoff.md — Handoff report
