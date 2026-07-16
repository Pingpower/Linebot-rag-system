# 診斷與根因分析報告 (Diagnostics & RCA Report)

## 核心發現 (Core Findings)
1. **本機 LLM 請求超出上下文限制 (400 Bad Request):**
   本機 LLaMA 伺服器 (`llama-server`) 啟動參數為 `--ctx-size 4096 --parallel 2`，這會將總 Context Size 平分給 2 個並行 Slot，使每個 Slot 的限制為 **2048 tokens** (`n_ctx = 2048`)。當 `app.py` 發送的 prompt（包含長 System Prompt、引導按鈕指令與歷史對話記錄）達到 **2876 tokens** 時，伺服器返回 400 Bad Request，觸發 `Exception` 並回覆用戶 `"抱歉，AI 服務暫時無法使用，請稍後再試。"。
2. **Gemini Embedding API 設定錯誤 (404 Not Found):**
   `semantic_cache.py` (第 24 行) 與 `sync_embeddings.py` (第 26 行) 使用的 Gemini Embedding API 請求路徑為 `v1beta`，然而 `text-embedding-004` 模型在 `v1beta` 下不可用，應使用 `v1` 路徑，導致所有 RAG 與語意快取功能失敗。

---

## 1. Supabase 連線與 Schema 調查

### Supabase 設定與連線狀態
在 `app.py` 中，Supabase 用戶端初始化如下：
- **程式碼檔案與行數:** `app.py` (第 48-57 行)
- **環境變數:**
  - `SUPABASE_URL="http://localhost:8000"` (本機 Docker/Supabase 服務)
  - `SUPABASE_SERVICE_KEY` 為有效的 Service Role Key，能繞過 RLS。
- **連線測試結果:** 經由測試腳本連線並成功查詢 `companies` 資料表，回傳：
  - `id`: `eb5dc6ac-972a-4528-a180-f96dce0fce12`
  - `slug`: `main`
  - `name`: `阿全村長服務站`
  - 連線運作完全正常。

### Schema 結構 (來自 `supabase_schema.sql`)
- 包含 `companies` (公司設定)、`knowledge_base` (知識庫向量資料)、`chat_history` (對話歷史紀錄)、`usage_logs` (使用記錄)、`semantic_cache` (語意快取) 等。
- 使用 `pgvector` 插件進行 `embedding vector(768)` 欄位設計。

---

## 2. OpenAI 用戶端版本與語法審查

- **庫版本:** 本機 Python 環境安裝的 `openai` 版本為 `2.36.0`。
- **語法相容性:**
  - `app.py` 使用了非同步用戶端：
    ```python
    llm_client = AsyncOpenAI(
        base_url="http://127.0.0.1:8080/v1",
        api_key="sk-no-key-required"
    )
    ```
  - 呼叫方法為 `await llm_client.chat.completions.create(...)`，異常捕獲為 `openai.APITimeoutError`、`openai.APIConnectionError`，語法完全正確且與 `openai>=1.0.0` 及 `2.x` 版本相容。
  - 連線測試證實，該語法能成功對接 `http://127.0.0.1:8080/v1` 的本機 LLM 服務，但在 Token 超出 2048 時會回傳 HTTP 400。

---

## 3. 詳細根因分析 (RCA)

### 根因 1: 400 Bad Request (Context Size Exceeded)
- **錯誤特徵:**
  ```
  2026-07-14 20:18:15,232 [ERROR] {'msg': 'LLM request failed', 'error': "Error code: 400 - {'error': {'code': 400, 'message': 'request (2876 tokens) exceeds the available context size (2048 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 2876, 'n_ctx': 2048}}"}
  ```
- **推導鏈結:**
  1. `llama-server` 啟動參數：`--ctx-size 4096 --parallel 2`。
  2. 每個Slot 的 context token 限制為 `4096 / 2 = 2048` tokens。
  3. `app.py` 在組合提示詞時包含：
     - `system_prompt` (基礎提示詞)。
     - `style_instruction` (極長的回覆風格約束，內含 FLEX_CARD JSON 範例，約 1200+ 字符)。
     - `assets_block` (公司自訂圖文資產)。
     - 對話歷史記錄 (預設拉取 5 輪，即 10 筆訊息)。
  4. 當前的 LLM 模型為 `gemma-4-12B-Distilled`。若歷史回覆中帶有長回覆或 Reasoning 思考區塊，會迅速將上下文撐爆至 2876 tokens，超出 2048 限制，導致 API 返回 400 錯誤。
  5. 400 錯誤被 `app.py` 的 `except Exception as e` 捕獲，並將回覆重設為固定錯誤訊息 `"抱歉，AI 服務暫時無法使用，請稍後再試。"。`

### 根因 2: Gemini Embedding 404 錯誤
- **錯誤特徵:**
  ```
  models/text-embedding-004 is not found for API version v1beta, or is not supported for embedContent.
  ```
- **程式碼問題:**
  - `semantic_cache.py` (第 24 行):
    ```python
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={gemini_key}"
    ```
  - `sync_embeddings.py` (第 26 行):
    ```python
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={GEMINI_KEY}"
    ```
  - Gemini API 的 `text-embedding-004` 必須透過 `v1` 路徑存取，使用 `v1beta` 會返回 404，導致 Embedding 無法生成，進而使 RAG 檢索與語意快取命中都失效。

---

## 建議修復方案 (Proposed Mitigations)
1. **修改 LLaMA 啟動參數:** 增加 `--ctx-size` 至 `8192` 或減少 `--parallel` 至 `1`，釋放單一 Slot 的 context 上限至 4096 tokens 以上。
2. **優化 app.py 歷史與 Prompt 長度:**
   - 優化 style 提示詞，精簡按鈕 JSON 範例。
   - 限制 `get_history` 歷史記錄只載入最近 3 輪（6 筆訊息）。
   - 過濾歷史記錄中的 `<think>...</think>` 區塊以節省空間。
3. **修復 Embedding API 端點:** 將 `v1beta` 改為 `v1`。
