# Handoff Report — 2026-07-14T20:35:00+08:00

## 1. Observation
During the investigation of `/home/pipadmin/文件/line_bot/`, we examined the code files and runtime log files for both `linebot-flask` (FastAPI app) and `linebot-llama` (llama-server).

### A. Llama Server Context Exceeded (Primary Cause)
In `/home/pipadmin/文件/flask.log` at lines 4149–4150:
```
2026-07-14 20:18:15,232 [INFO] HTTP Request: POST http://127.0.0.1:8080/v1/chat/completions "HTTP/1.1 400 Bad Request"
2026-07-14 20:18:15,232 [ERROR] {'msg': 'LLM request failed', 'error': "Error code: 400 - {'error': {'code': 400, 'message': 'request (2876 tokens) exceeds the available context size (2048 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 2876, 'n_ctx': 2048}}"}
```
In `/home/pipadmin/文件/line_bot/app.py` at lines 1082–1085:
```python
    except Exception as e:
        logger.error({"msg": "LLM request failed", "error": str(e)})
        ai_reply = "抱歉，AI 服務暫時無法使用，請稍後再試。"
        clean_reply = ai_reply
```

In `linebot-llama.service` configuration file `/home/pipadmin/.config/systemd/user/linebot-llama.service` at line 8:
```ini
ExecStart=/home/pipadmin/文件/llama.cpp/build/bin/llama-server --model /home/pipadmin/文件/models/gemma-4-12B-it-QAT-Q4_0.gguf --host 127.0.0.1 --port 8080 --ctx-size 4096 --n-gpu-layers 99 --threads 6 --threads-batch 6 --parallel 2 --cache-type-k q8_0 --cache-type-v q8_0 --no-mmap --mlock --flash-attn on --log-disable
```

### B. Gemini Embedding API 404 (Secondary Cause)
In `/home/pipadmin/文件/flask.log` at lines 4128–4135 (and similarly in `sync_embeddings.py` execution logs):
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
In `/home/pipadmin/文件/line_bot/semantic_cache.py` at line 24:
```python
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={gemini_key}"
```
In `/home/pipadmin/文件/line_bot/sync_embeddings.py` at line 26:
```python
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={GEMINI_KEY}"
```

---

## 2. Logic Chain
1. **Root Cause of user error**: When a user sends a message, `app.py` constructs a prompt including the system prompt, guidelines, company assets, context documents, and conversation history, which easily reaches over 2500 tokens (observed to be 2876 tokens).
2. **Context split in llama-server**: The `llama-server` is configured with a total context size of `--ctx-size 4096` and slots `--parallel 2`. In llama-server, the context is split evenly across slots, resulting in an effective context limit of `2048` tokens per slot.
3. **LLM request failure**: Because the request (2876 tokens) exceeds the slot's limit (2048 tokens), llama-server returns a `400 Bad Request`.
4. **FastAPI application fallback**: `app.py` catches this exception under the generic `except Exception as e:` block and defaults the reply to `"抱歉，AI 服務暫時無法使用，請稍後再試。"`.
5. **Embedding failure**: The semantic cache and knowledge synchronization scripts query Gemini API using `v1beta` version for the `text-embedding-004` model. Gemini returns `404 Not Found` because `text-embedding-004` is not registered/supported in `v1beta`.
6. **No cache hits**: Due to the embedding API failure, query embeddings are never successfully generated, which invalidates semantic cache lookups. Every single request is forced to go to the local LLM, compounding the context exceeded errors.

---

## 3. Caveats
- Checked GPU/CUDA errors in older logs. The CUDA crashes have been resolved and the server is running stable now, but the context limit configuration issue remains.
- Presumed that the client intends to continue using two parallel slots. If parallel slots are not needed, `--parallel 1` could be used.

---

## 4. Conclusion
To resolve the "AI 服務暫時無法使用" error:
1. **Increase Llama Context Size**: In `/home/pipadmin/.config/systemd/user/linebot-llama.service`, change `--ctx-size 4096` to `--ctx-size 8192` (which allocates `4096` tokens per slot for `--parallel 2`).
2. **Update Gemini API Version**: In both `/home/pipadmin/文件/line_bot/semantic_cache.py` and `/home/pipadmin/文件/line_bot/sync_embeddings.py`, update the endpoint URL from `/v1beta/` to `/v1/` to fix the 404 embedding error.

---

## 5. Verification Method
1. **Verify Llama service update**:
   - Apply the `--ctx-size 8192` modification to the systemd service file.
   - Run:
     ```bash
     systemctl --user daemon-reload
     systemctl --user restart linebot-llama.service
     ```
   - Confirm in `ps -fp <pid>` or logs that llama-server is running with `--ctx-size 8192`.
2. **Verify Embedding API update**:
   - Change the URL in `sync_embeddings.py` to `/v1/`.
   - Run `python3 sync_embeddings.py` and ensure the 404 error is resolved and embeddings sync successfully.
