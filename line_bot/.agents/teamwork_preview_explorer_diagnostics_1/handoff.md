# Handoff Report - Diagnostic Investigation (LINE Bot AI Reply Error)

This handoff contains all observations, logic chains, caveats, conclusions, and verification methods to fix the LINE Bot AI reply issue.

---

## 1. Observation

We directly observed the following on the target environment:

- **Running Services:**
  - `ss -tlnp` shows `llama-server` is listening on `127.0.0.1:8080` (PID 1567) and the FastAPI app is listening on `0.0.0.0:5000` (PID 1568).
  - The process for `llama-server` is:
    ```
    /home/pipadmin/文件/llama.cpp/build/bin/llama-server --model /home/pipadmin/文件/models/gemma-4-12B-it-QAT-Q4_0.gguf --host 127.0.0.1 --port 8080 --ctx-size 4096 --n-gpu-layers 99 --threads 6 --threads-batch 6 --parallel 2 --cache-type-k q8_0 --cache-type-v q8_0 --no-mmap --mlock --flash-attn on --log-disable
    ```
- **Error in `flask.log` (FastAPI Application logs):**
  - Path: `/home/pipadmin/文件/flask.log`
  - On July 14, 20:18:15, a call to the LLM failed with:
    ```
    2026-07-14 20:18:15,232 [ERROR] {'msg': 'LLM request failed', 'error': "Error code: 400 - {'error': {'code': 400, 'message': 'request (2876 tokens) exceeds the available context size (2048 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 2876, 'n_ctx': 2048}}"}
    ```
  - On July 14, 20:18:14, the Gemini embedding API call failed with a 404:
    ```
    2026-07-14 20:18:14,702 [ERROR] Semantic Cache: Gemini API Error: 404 - {
      "error": {
        "code": 404,
        "message": "models/text-embedding-004 is not found for API version v1beta, or is not supported for embedContent. Call ModelService.ListModels to see the list of available models and their supported methods.",
        "status": "NOT_FOUND"
      }
    }
    ```
- **Code Configurations:**
  - File: `/home/pipadmin/文件/line_bot/semantic_cache.py` (Line 24) and `/home/pipadmin/文件/line_bot/sync_embeddings.py` (Line 26) use:
    `url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key=..."`
- **Supabase Credentials:**
  - File: `/home/pipadmin/文件/line_bot/.env` points to `SUPABASE_URL="http://localhost:8000"`. Connection logs show successful connection.

---

## 2. Logic Chain

1. **Context Limit Overflow:**
   - The llama-server was started with `--ctx-size 4096 --parallel 2`.
   - In llama-server, setting `--parallel 2` splits the total context space (`4096` tokens) among the slots, giving each slot only `2048` tokens maximum context.
   - The prompt constructed by `app.py` totals `2876` tokens (consisting of the long system prompt instructions, custom card examples, retrieval documents, history, and the user message).
   - Because `2876 > 2048`, the server rejects the request with a `400 Bad Request`.
   - `app.py` catches this exception and replies `"抱歉，AI 服務暫時無法使用，請稍後再試。"`.

2. **Gemini Embedding Endpoint Version Misconfiguration:**
   - The code requests `text-embedding-004` on the `v1beta` API endpoint.
   - The model `text-embedding-004` is not supported on the `v1beta` endpoint and returns a `404 Not Found` response.
   - Consequently, query embedding generation fails, returning empty cache and search results.

---

## 3. Caveats

- We did not test outbound network traffic to the Gemini API due to operating in `CODE_ONLY` network mode, but the detailed API server error log (404 NOT_FOUND) indicates that communication is reaching Google's servers but using an invalid API endpoint version.
- Hardware limit constraints were not checked, but the GTX 1060 GPU runs partially offloaded CPU-GPU inference, so increasing context sizes will work fine but might incur slight performance/speed overhead on CPU.

---

## 4. Conclusion

The bot failure is due to a **context overflow (400 Bad Request)** in the local `llama-server` and a **misconfigured embedding API version (404 Not Found)**. 

### Proposed Fix Actions:
1. In `/home/pipadmin/.config/systemd/user/linebot-llama.service`, change `--ctx-size 4096` to `--ctx-size 8192` (or adjust `--parallel 2` to `--parallel 1` or similar), then reload and restart `linebot-llama.service`.
2. In `/home/pipadmin/文件/line_bot/semantic_cache.py` (Line 24) and `/home/pipadmin/文件/line_bot/sync_embeddings.py` (Line 26), update the URL from `/v1beta/` to `/v1/`, then restart `linebot-flask.service`.

---

## 5. Verification Method

- Run `python3 /home/pipadmin/文件/line_bot/sync_embeddings.py` to confirm that embedding generation succeeds without 404 errors.
- Run a mockup request to the LLM to verify it can process inputs longer than 2048 tokens successfully.
