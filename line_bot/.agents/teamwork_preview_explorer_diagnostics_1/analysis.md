# Diagnostic Investigation Analysis Report

This report presents findings from investigating the LINE Bot AI service error: "抱歉，AI 服務暫時無法使用，請稍後再試。"

---

## 1. Observation

We directly observed the following:

1. **Active Listening Ports and Processes:**
   - Command `ss -tlnp` output:
     ```
     LISTEN  0       2048                         0.0.0.0:5000         0.0.0.0:*      users:(("python3",pid=1568,fd=6))
     LISTEN  0       512                        127.0.0.1:8080         0.0.0.0:*      users:(("llama-server",pid=1567,fd=28))
     ```
   - Command `ps -ww -fp 1567` output showing the running `llama-server`:
     ```
     /home/pipadmin/文件/llama.cpp/build/bin/llama-server --model /home/pipadmin/文件/models/gemma-4-12B-it-QAT-Q4_0.gguf --host 127.0.0.1 --port 8080 --ctx-size 4096 --n-gpu-layers 99 --threads 6 --threads-batch 6 --parallel 2 --cache-type-k q8_0 --cache-type-v q8_0 --no-mmap --mlock --flash-attn on --log-disable
     ```
   - Command `ps -ww -fp 1568` output showing the running `FastAPI` app:
     ```
     /usr/bin/python3 /home/pipadmin/文件/line_bot/app.py
     ```

2. **Systemd Services Status:**
   - Both services are active and running as systemd user units:
     - `linebot-flask.service` (representing the python FastAPI application)
     - `linebot-llama.service` (representing the llama-server)

3. **Verbatim Error Log from `flask.log` on Today's Failed LLM Call (July 14, 20:18:15):**
   - File path: `/home/pipadmin/文件/flask.log`
     - Line 4149:
       ```
       2026-07-14 20:18:15,232 [INFO] HTTP Request: POST http://127.0.0.1:8080/v1/chat/completions "HTTP/1.1 400 Bad Request"
       ```
     - Line 4150:
       ```
       2026-07-14 20:18:15,232 [ERROR] {'msg': 'LLM request failed', 'error': "Error code: 400 - {'error': {'code': 400, 'message': 'request (2876 tokens) exceeds the available context size (2048 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 2876, 'n_ctx': 2048}}"}
       ```

4. **Verbatim Error Log from `flask.log` on Gemini API Embedding (July 14, 20:18:14):**
   - File path: `/home/pipadmin/文件/flask.log`
     - Lines 4128–4134:
       ```
       2026-07-14 20:18:14,701 [INFO] HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key=AIzaSyC9H32xNrTUQnetbHzN83SGJ5627jSyF2M "HTTP/1.1 404 Not Found"
       2026-07-14 20:18:14,702 [ERROR] Semantic Cache: Gemini API Error: 404 - {
         "error": {
           "code": 404,
           "message": "models/text-embedding-004 is not found for API version v1beta, or is not supported for embedContent. Call ModelService.ListModels to see the list of available models and their supported methods.",
           "status": "NOT_FOUND"
         }
       }
       ```

5. **Embedding Endpoint Configurations:**
   - File: `/home/pipadmin/文件/line_bot/semantic_cache.py` (Line 24):
     ```python
     url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={gemini_key}"
     ```
   - File: `/home/pipadmin/文件/line_bot/sync_embeddings.py` (Line 26):
     ```python
     url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={GEMINI_KEY}"
     ```

6. **Supabase Local Connection Credentials:**
   - File: `/home/pipadmin/文件/line_bot/.env` (Lines 11–16):
     ```
     SUPABASE_URL="http://localhost:8000"
     SUPABASE_SERVICE_KEY="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
     ```
   - Logs confirm successful connections (status code `200` and `201`) to the local Supabase instance.

---

## 2. Logic Chain

1. **LLM Server Connection Failure:**
   - From **Observation 3**, calling the local llama-server on port 8080 returned a `400 Bad Request` with an explicit message: `request (2876 tokens) exceeds the available context size (2048 tokens)`.
   - From **Observation 1**, the `llama-server` is started with `--ctx-size 4096 --parallel 2`.
   - In llama-server, when `--parallel 2` is set, the total context (`--ctx-size 4096`) is split equally among the slots, limiting each parallel slot's maximum context size to `4096 / 2 = 2048` tokens.
   - The LINE Bot constructing the prompt sends a payload totaling `2876` tokens (consisting of the system prompt instructions, retrieved knowledge files, history, and the user message). Since `2876 > 2048`, it is rejected by llama-server, throwing a connection error exception in `app.py`.
   - In `app.py`, this exception fallback triggers the response `"抱歉，AI 服務暫時無法使用，請稍後再試。"`.

2. **Embedding API Failure:**
   - From **Observation 4**, the Gemini API embedding generation fails with a `404 Not Found` response.
   - From **Observation 5**, the embedding API URL queries the endpoint `v1beta/models/text-embedding-004`.
   - The Gemini API does not support `text-embedding-004` on the `v1beta` endpoint; the model must be queried using the standard `v1` endpoint.
   - Due to this failure, embedding generation fails, causing both semantic cache lookup and hybrid RAG searches to return empty results. Even though this does not crash the app, it degrades quality and prevents correct document retrieval.

---

## 3. Caveats

- We did not verify the validity of the Google Gemini API key because we are operating in `CODE_ONLY` network mode, preventing us from making outbound internet requests. However, the `404 Not Found` error returned from Google's server indicates that the HTTP request did reach Google's API, and the error was specifically about the model configuration/path, not an authentication failure.
- We assumed no other hardware memory limits restrict the context size expansion on the host. However, the GTX 1060 (6GB VRAM) running a 12B model already offloads computations to the CPU, so increasing the context size might slightly slow down generation speed or increase CPU RAM usage.

---

## 4. Conclusion

The user-facing error "抱歉，AI 服務暫時無法使用，請稍後再試。" is directly caused by a **context limit overflow** in the local `llama-server` (returning a `400 Bad Request`), triggered by the combined size of the system instructions, RAG context, and history exceeding the slot limit of 2048 tokens. 

Additionally, the **Gemini embedding API endpoint is misconfigured** (using `v1beta` instead of `v1`), causing all vector/hybrid searches to fail and return empty results.

---

## 5. Proposed Fix Strategy

### Fix 1: Adjust LLaMA Server Context Configurations
In the systemd service file `/home/pipadmin/.config/systemd/user/linebot-llama.service`, modify the `ExecStart` line to either:
- **Increase total context size** from `--ctx-size 4096` to `--ctx-size 8192` (each slot will get `8192 / 2 = 4096` tokens).
- **OR decrease concurrency** from `--parallel 2` to `--parallel 1` (allowing the single slot to use the full `4096` tokens).
- After the change, reload and restart the user service:
  ```bash
  systemctl --user daemon-reload
  systemctl --user restart linebot-llama.service
  ```

### Fix 2: Correct Gemini Embedding API Endpoint Version
In `/home/pipadmin/文件/line_bot/semantic_cache.py` (Line 24) and `/home/pipadmin/文件/line_bot/sync_embeddings.py` (Line 26), update the URL endpoint from `v1beta` to `v1`:
- **Before:**
  `https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key=...`
- **After:**
  `https://generativelanguage.googleapis.com/v1/models/text-embedding-004:embedContent?key=...`
- After the change, restart the FastAPI service:
  ```bash
  systemctl --user restart linebot-flask.service
  ```

---

## 6. Verification Method

1. **Verify LLaMA Context Slot Limit:**
   - Execute a mock curl command to the local LLM after restarting the server to verify it accepts prompts longer than 2048 tokens:
     ```bash
     curl -X POST http://127.0.0.1:8080/v1/chat/completions \
       -H "Content-Type: application/json" \
       -d '{"model": "local-model", "messages": [{"role": "user", "content": "'$(head -c 12000 < /dev/zero | tr '\0' 'A')'"}]}'
     ```
     *Expected result: Status 200 OK or processing start without context length error.*

2. **Verify Gemini Embedding Call:**
   - Run the synchronization script to verify embedding generation runs successfully:
     ```bash
     python3 /home/pipadmin/文件/line_bot/sync_embeddings.py
     ```
     *Expected result: "Successfully updated X embeddings" without Gemini API 404 errors.*
