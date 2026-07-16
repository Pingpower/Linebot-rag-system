# Handoff Report

## 1. Observation (觀察結果)
- **Supabase 連線與設定:**
  - `app.py` 中初始化 `create_client` (第 48-57 行)，對接環境變數中的 `SUPABASE_URL` 與 `SUPABASE_SERVICE_KEY`。
  - 使用本機系統 Python 環境測試，成功查詢 `companies` 資料表，獲得 slug 為 `'main'` 的公司資料 (阿全村長服務站)。
- **LLM 請求失敗日誌 (來自 `/home/pipadmin/文件/flask.log`):**
  - 行 4150:
    ```
    2026-07-14 20:18:15,232 [ERROR] {'msg': 'LLM request failed', 'error': "Error code: 400 - {'error': {'code': 400, 'message': 'request (2876 tokens) exceeds the available context size (2048 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 2876, 'n_ctx': 2048}}"}
    ```
- **本機 LLaMA 服務啟動參數 (來自 `/home/pipadmin/.config/systemd/user/linebot-llama.service`):**
  - 行 8:
    ```
    ExecStart=/home/pipadmin/文件/llama.cpp/build/bin/llama-server --model /home/pipadmin/文件/models/gemma-4-12B-it-QAT-Q4_0.gguf --host 127.0.0.1 --port 8080 --ctx-size 4096 --n-gpu-layers 99 --threads 6 --threads-batch 6 --parallel 2 ...
    ```
- **Gemini Embedding API 失敗日誌 (來自 `/home/pipadmin/文件/flask.log`):**
  - 行 4128:
    ```
    2026-07-14 20:18:14,701 [INFO] HTTP Request: POST https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key=... "HTTP/1.1 404 Not Found"
    2026-07-14 20:18:14,702 [ERROR] Semantic Cache: Gemini API Error: 404 - { ... "message": "models/text-embedding-004 is not found for API version v1beta, or is not supported for embedContent." }
    ```
- **Embedding API 路徑寫法:**
  - `sync_embeddings.py` 第 26 行：`url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={GEMINI_KEY}"`
  - `semantic_cache.py` 第 24 行：`url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={gemini_key}"`

---

## 2. Logic Chain (推理鏈)
1. **問題起因:** 使用者向 LINE Bot 發送訊息時，Bot 回覆 `"抱歉，AI 服務暫時無法使用，請稍後再試。"`。
2. **定位錯誤源頭:** 在 `app.py` 中，此回覆由異常處理區塊（行 1074-1085）捕獲任意例外時拋出。
3. **查閱日誌:** 日誌 `/home/pipadmin/文件/flask.log` 顯示在 2026-07-14 20:18:15.232，LLM 請求因 **HTTP 400 Bad Request** 失敗，錯誤內容指明 prompt 有 `2876` tokens，超出了可用 context size `2048` (`n_ctx = 2048`)。
4. **比對服務配置:**
   - 檢視 `linebot-llama.service` 發現 `llama-server` 啟動參數配置 `--ctx-size 4096 --parallel 2`。
   - `llama.cpp` 中並行處理 Slot 的計算邏輯為：單個 Slot 容量限制 = 總容量 / 並行度。因此單一並行通道 context 限制為 `4096 / 2 = 2048` tokens。
5. **Prompt 膨脹成因:** `app.py` 組合了極具約束性的 `style_instruction`、自訂 `assets_block`，以及最近 5 輪對話歷史。模型在之前的回覆中可能吐出較長的內容，且載入歷史時未進行過濾，導致 Token 數增長至 2876，超出 2048 限制，必然引起 400 報錯。
6. **Gemini 404 發現:** 另外，RAG 因 Gemini 請求 `v1beta` 版的 `text-embedding-004` 被拒而拋出 404 錯誤，返回空 RAG 結果，雖未直接阻斷程式，但使檢索功能失效。

---

## 3. Caveats (注意事項)
- **環境未修改:** 我們僅執行了唯讀調查，沒有直接修改任何程式碼或服務啟動檔。
- **網路限制:** 處於 `CODE_ONLY` 模式，無法對外呼叫 Gemini API 驗證 v1 端點是否能正常簽發該 API key。但本地 Supabase 與 LLM 連線均已在本地模擬證實完全可行。

---

## 4. Conclusion (結論)
錯誤是由於 **(1) llama-server 並行 slot 限制導致單次請求 Context 超限 (2048 tokens)** 以及 **(2) Gemini Embedding 請求路徑（v1beta 改 v1）錯誤** 所引起。
這兩者導致 LLM 生成請求時發送 400 錯誤，而被 `app.py` 的全域例外捕獲，最終回覆給用戶 `"抱歉，AI 服務暫時無法使用，請稍後再試。"。`

---

## 5. Verification Method (驗證方法)
1. **本地執行檢測:**
   ```bash
   /usr/bin/python3 /home/pipadmin/文件/line_bot/.agents/teamwork_preview_explorer_diagnostics_2/test_diagnostics.py
   ```
2. **觀察日誌輸出:**
   確認 Supabase companies 能被檢索、`http://127.0.0.1:8080/v1` 的 models list 正常回傳，並注意當 Context token 長度小於 2048 時，LLM 請求能正常成功；當大於 2048 時則必定失敗。
