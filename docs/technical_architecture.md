# 🚀 LINE Bot & RAG 核心技術與架構白皮書

這份文件詳細記錄了我們在打造 LINE Bot 智慧客服系統時所採用的尖端技術、自研演算法以及系統特點。

## 1. 混合式 RAG (Hybrid Retrieval-Augmented Generation) 演算法
為了打破單一向量搜尋 (Vector Search) 容易忽略精確關鍵字的缺點，我們結合了多重搜尋策略：
*   **Gemini Embedding 2 向量化**：將知識庫文章轉換為 768 維度的高維度向量，確保跨語意與上下文的理解（例如「補助」與「津貼」能被辨識為相似意圖）。
*   **BM25 / FTS 全文檢索**：保留傳統關鍵字搜尋，確保對特殊名詞、編號、法條的「精確匹配」。
*   **Reciprocal Rank Fusion (RRF)**：使用 Supabase Postgres 的 RPC 函數 `match_knowledge_hybrid`，在資料庫層級透過 RRF 演算法將「向量分數」與「全文檢索分數」進行平滑融合，輸出最終的最優排序結果。

## 2. 🚀 三層式智慧語意快取 (Multi-Layer Semantic Cache)
為了降低 API 呼叫成本與減少系統延遲 (Latency)，我們自研了三層過濾的 Cache 演算法：
1.  **L1 - 原始精確匹配 (Raw Exact Match)**：直接比對用戶輸入，專門攔截 LINE Flex Message 選單按鈕的制式點擊，達到 `~50ms` 的零成本秒回。
2.  **L2 - 降噪精確匹配 (Normalized Match)**：運用正則表達式剝離用戶口語贅字（如：「請問」、「幫我查」、「...呢」）及標點符號後進行精確比對。大幅提高因語氣變化導致的 Cache Miss 情況。
3.  **L3 - 向量語意匹配 (Vector Semantic Match)**：若精確比對皆未命中，則呼叫 Gemini API 產生句向量，與快取庫進行 Cosine Similarity 比對（門檻 0.92）。
*   **短句旁路優化 (Bypass Gate)**：為了防止向量搜尋將短字串（低於 12 個字）誤判為高相似度的通用概念，系統在 L2 之後若判斷為短句，會自動跳過 L3 向量匹配，避免「按鈕點擊」引發不可預期的錯誤回答。

## 3. 🧠 Agentic Memory 長期記憶與動態查詢擴展
這不再是一個單向問答的系統，我們引入了「代理人 (Agent)」機制的背景運作：
*   **特徵萃取代理 (Memory Update Agent)**：使用者每次對話時，系統會在背景喚醒微型 LLM 任務，自動判讀對話中是否包含用戶個人特徵（如：年齡、居住地、子女數量），並自動 JSON 化寫入 Supabase `user_profiles`，賦予系統跨對話的長期記憶。
*   **查詢擴寫代理 (Query Expansion Agent)**：在進入 RAG 檢索前，動態依據歷史對話上下文，將簡短問題擴寫為完整描述，並提取具體的 Metadata Tags（如 `分類:補助`），以此作為混合檢索的 Filter 參數。

## 4. 🛡️ 推理洩漏防禦機制 (Reasoning Guard)
針對大型推理模型（如 DeepSeek-R1、Llama-3-Reasoning）在進行複雜推論時容易洩漏 Chain-of-Thought (思考過程) 的問題：
*   **強制封裝邊界**：在系統提示詞嚴格約束使用 `[FINAL_ANSWER]` 分隔內部推演與外部輸出。
*   **斷尾防護網**：引入針對未閉合 `<think>` 標籤的正則切割 `(?:</think>|$)`，以及針對 `reasoning_content` API 回傳欄位的雙重阻斷，確保即便模型遇上 Token 耗盡 (Max Tokens) 的極端情況，也絕對不會把系統內部機密與思考歷程噴給最終用戶。

## 5. 🏢 真實多租戶架構 (True Multi-Tenancy)
*   透過 URL 動態路由 (`/callback/<company_slug>`) 區分不同企業客戶的 LINE Webhook。
*   所有的 Supabase 資料表（`companies`, `knowledge_base`, `chat_history`, `semantic_cache`, `user_profiles`）皆強制綁定 `company_id`，結合 Row Level Security (RLS) 或伺服器端的 Filter，實現同一套部署支撐無數個相互獨立的 LINE 客服機器人。

## 6. 📜 ADR 技術決策快照 (Architecture Decision Records)
專案內建 `.agent/decisions/` 決策紀錄庫。所有重大演算法更迭（例如：使用 HTML5 Dataset 取代複雜狀態管理、雙層快取決策等）都會被記錄為不可變更的 ADR 文件，賦予專案極高的維護性與團隊傳承能力。
