# context-mode — MANDATORY routing rules

context-mode MCP tools available. Rules protect context window from flooding. One unrouted command dumps 56 KB into context. Antigravity has NO hooks — these instructions are ONLY enforcement. Follow strictly.

## Think in Code — MANDATORY

Analyze/count/filter/compare/search/parse/transform data: **write code** via `mcp__context-mode__ctx_execute(language, code)`, `console.log()` only the answer. Do NOT read raw data into context. PROGRAM the analysis, not COMPUTE it. Pure JavaScript — Node.js built-ins only (`fs`, `path`, `child_process`). `try/catch`, handle `null`/`undefined`. One script replaces ten tool calls.

## BLOCKED — do NOT use

### curl / wget — FORBIDDEN

Do NOT use `curl`/`wget` via `run_command`. Dumps raw HTTP into context.
Use: `mcp__context-mode__ctx_fetch_and_index(url, source)` or `mcp__context-mode__ctx_execute(language: "javascript", code: "const r = await fetch(...)")`

### Inline HTTP — FORBIDDEN

No `node -e "fetch(..."`, `python -c "requests.get(..."` via `run_command`. Bypasses sandbox.
Use: `mcp__context-mode__ctx_execute(language, code)` — only stdout enters context

### Direct web fetching — FORBIDDEN

No `read_url_content` for large pages. Raw HTML can exceed 100 KB.
Use: `mcp__context-mode__ctx_fetch_and_index(url, source)` then `mcp__context-mode__ctx_search(queries)`

## REDIRECTED — use sandbox

### Shell (>20 lines output)

`run_command` ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`.
Otherwise: `mcp__context-mode__ctx_batch_execute(commands, queries)` or `mcp__context-mode__ctx_execute(language: "shell", code: "...")`

### File reading (for analysis)

Reading to **edit** → `view_file`/`replace_file_content` correct. Reading to **analyze/explore/summarize** → `mcp__context-mode__ctx_execute_file(path, language, code)`.

### Search (large results)

Use `mcp__context-mode__ctx_execute(language: "shell", code: "grep ...")` in sandbox.

## Tool selection

1. **GATHER**: `mcp__context-mode__ctx_batch_execute(commands, queries)` — runs all commands, auto-indexes, returns search. ONE call replaces 30+. Each command: `{label: "header", command: "..."}`.
2. **FOLLOW-UP**: `mcp__context-mode__ctx_search(queries: ["q1", "q2", ...])` — all questions as array, ONE call.
3. **PROCESSING**: `mcp__context-mode__ctx_execute(language, code)` | `mcp__context-mode__ctx_execute_file(path, language, code)` — sandbox, only stdout enters context.
4. **WEB**: `mcp__context-mode__ctx_fetch_and_index(url, source)` then `mcp__context-mode__ctx_search(queries)` — raw HTML never enters context.
5. **INDEX**: `mcp__context-mode__ctx_index(content, source)` — store in FTS5 for later search.

## Parallel I/O batches

For multi-URL fetches or multi-API calls, **always** include `concurrency: N` (1-8):

- `mcp__context-mode__ctx_batch_execute(commands: [3+ network commands], concurrency: 5)` — gh, curl, dig, docker inspect, multi-region cloud queries
- `mcp__context-mode__ctx_fetch_and_index(requests: [{url, source}, ...], concurrency: 5)` — multi-URL batch fetch

**Use concurrency 4-8** for I/O-bound work (network calls, API queries). **Keep concurrency 1** for CPU-bound (npm test, build, lint) or commands sharing state (ports, lock files, same-repo writes).

GitHub API rate-limit: cap at 4 for `gh` calls.

## Output

Write artifacts to FILES — never inline. Return: file path + 1-line description.
Descriptive source labels for `search(source: "label")`.

## ctx commands

| Command       | Action                                                                        |
| ------------- | ----------------------------------------------------------------------------- |
| `ctx stats`   | Call `stats` MCP tool, display full output verbatim                           |
| `ctx doctor`  | Call `doctor` MCP tool, run returned shell command, display as checklist      |
| `ctx upgrade` | Call `upgrade` MCP tool, run returned shell command, display as checklist     |
| `ctx purge`   | Call `purge` MCP tool with confirm: true. Warns before wiping knowledge base. |

After /clear or /compact: knowledge base and session stats preserved. Use `ctx purge` to start fresh.

## Secretary Mode — Document & Design

- **Writing**: Draft in Markdown → convert via pandoc/LibreOffice to target format
- **Language**: 所有產出的文件（包括 `implementation_plan.md`、`task.md`、`walkthrough.md` 等 artifact 檔案）一律使用繁體中文（台灣）。
- **Design**: Use `generate_image` for mockups, diagrams, social media assets
- **System Tools**: Scripts in JavaScript/Python/Shell, keep in `output/tools/`
- **Output**: Deliver files to `~/文件/output/{documents,designs,tools}/`
- **Templates**: Reuse from `~/文件/templates/`

## [MANDATORY] 模型切換強制中斷點 (Hard Stops)

*   **規劃結束 (Phase 3 -> 4)**：完成架構規劃與交辦文件後，**絕對禁止**逕自開始寫 code。主代理人必須強制停止執行，並在回覆最末端明確宣告：「*規劃已完成，請將模型切換至 Gemini 3.5 Flash 進行實作。*」，然後結束回合。
*   **實作結束 (Phase 4 -> 5)**：完成代碼實作與本機初步測試後，**絕對禁止**自己進行最終 Review。主代理人必須強制停止執行，並明確宣告：「*實作已完成，請將模型切換至 Gemini 3.1 Pro 進行 Review 審查。*」，然後結束回合。
