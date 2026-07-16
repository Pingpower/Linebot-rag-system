# Semantic Cache 實作交辦文件 (Handoff Doc)

## 任務目標
優化 `line_bot/semantic_cache.py` 以提升語意快取的命中率 (Hit Rate)。

## 實作細節

1. **實作策略一：查詢前處理與降噪 (Query Normalization)**
   - 在 `semantic_cache.py` 中新增一個 `normalize_query(text: str) -> str` 的輔助函式。
   - 使用 Regex 移除常見的無效語氣詞（如：「請問」、「幫我查一下」、「謝謝」、「告訴我」）與標點符號。
   - 在 `check_cache` 與 `add_to_cache` 中，於取得 Embedding 前先對 `query_text` 呼叫此函式降噪。
   - **注意**：存入 Supabase 的 `query_text` 欄位可以保留原始文字供人類閱讀，但用於算 Embedding 的字串必須是降噪後的。

2. **實作策略四：雙層過濾機制 (Exact Match)**
   - 在 `check_cache` 函式開頭（呼叫 Gemini API 算 Embedding 之前），先對資料庫做一次精確字串比對 (Exact Text Match)。
   - 可以查詢 `semantic_cache` 資料表中，`company_id` 相符且 `query_text` 等於原始文字（或降噪後文字）的有效紀錄。
   - 如果找到，直接回傳 `reply_data`，**跳過後續所有的 Embedding 計算與相似度比對**。
   - 這能省下大量重複點擊選單按鈕的 API 成本與延遲。

## 注意事項
- 目前在 `Gemini 3.5 Flash` 模型進行實作。
- 確保所有函式維持 `async` 非同步架構，不破壞原有的 FastAPI 效能。
- 實作完成後，務必啟動 `line_bot/app.py` 進行測試，確認沒有 syntax error。
