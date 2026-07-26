# LINE Bot & 後台管理系統 (以本地大模型與 RAG 驅動)

本專案是一個整合了本地大語言模型（LLM）與檢索增強生成（RAG）技術的 LINE Bot 客服與智慧後台管理系統。

## 系統核心架構

- **LINE Bot (`line_bot/`)**：基於 Flask 框架開發，採用本地中文字元分詞與二元 N-Gram 比對算法實現模糊檢索。檢索完成後呼叫本地部署的大模型 API 生成人性化的解答。
- **後台控制台 (`admin/`)**：提供視覺化的 Web 管理面板（Port 8888）。具備合作企業管理、自訂參數配置、知識庫管理與 AI-Collect 自動化網頁知識萃取等功能。
- **本地推論引擎 (llama-server)**：透過 `llama.cpp` 載入 GGUF 格式的模型檔案，並藉由 CUDA 進行顯示卡加速推論。

---

## 最新功能亮點 (2026-05-23 更新)

1. **混合式原生 Flex Message 渲染 (Silent Card / Carousel 輪播)**：
   - 系統現在完美相容 LINE 官方原生 Flex JSON。當 `[FLEX_CARD]` 標籤中包含 `"type": "bubble"` 或 `"type": "carousel"` 時，自動解析渲染，支援精巧的 Silent Card 與多張輪播橫滑卡片。
2. **彈性引導式對話**：
   - 針對長篇大論的業務解答進行智慧按鈕分流，預設傾向於 3 層內引導完畢以維持良好體驗。此機制具備接續問答彈性，若用戶在 3 層後繼續提問或追問，AI 將持續以 [FLEX_CARD] 圖卡引導，不會退回無 Flex 樣式的純文字。
3. **模型分級參數自動調配**：
   - 管理後台在熱切換模型時，會自動依據模型大小（Tiny、Medium、Large）調整 `llama-server` 啟動參數（GPU 載入層數 `gpu_layers` 與 `ctx_size`），避免 6GB 顯卡 (GTX 1060) 顯存溢出，同時最大化加速小模型。
4. **對話歷史乾淨化存檔**：
   - 儲存到 Supabase 的歷史紀錄會自動過濾 `[FLEX_CARD]` 內部 JSON 與所有 Markdown `**` 等符號，確保後台對話紀錄的純文字清晰易讀。
5. **知識庫整理多模型 API 路由支援**：
   - 後台上傳並結構化文檔（萃取知識條目）時，除了支援本地運行模型外，現在也能動態分流至 Google Gemini、NVIDIA NIM 或 OpenRouter 等外部高階 LLM API。極大提升了複雜長文檔在背景切分與整理的成效，且不佔用本地顯存資源。
6. **AI-Collect 智慧資料蒐集四大功能**：
   - 後台支援「網址爬取與目錄結構探測分段匯入」、「多格式檔案上傳解析（PDF/Word）」、「線上主題關鍵字搜尋萃取」與「純文字文稿直接整理」等 4 大快速條目產生功能，並支援對話引導按鈕與適用對象之 Metadata 設定。

---

## 快速開始與常用指令

專案內置了多個便於部署與運作的自動化指令腳本：

### 1. 互動式更換模型

當需要更換 AI 模型時，請於工作區執行：

```bash
bash select_model.sh
```

此腳本將自動掃描 `models/` 下的 `.gguf` 檔案，供您選擇，並自動更新並重啟 Systemd 服務。

### 2. 啟動整個系統服務

```bash
bash start_system.sh
```

此指令會一次性啟動模型推論、LINE Bot、管理後台以及 Cloudflare 穿透服務。

### 3. 打包備份環境

若需移植專案至其他伺服器，可執行以下指令將代碼、設定檔及 Systemd 服務打包（會自動排除 `.env`、大型模型檔與虛擬環境）：

```bash
bash backup_env.sh
```

---

## 最新核心技術與特色 (2026-07-17 更新)

本專案除了基礎的 RAG 運作外，更深入優化了各項演算法與架構：

1. **🏢 真實多租戶架構 (True Multi-Tenancy)**：透過 Supabase Row Level Security (RLS) 與動態 Webhook，單一伺服器可同時服務多間企業，彼此資料完全隔離。
2. **🚀 三層式智慧語意快取 (Multi-Layer Semantic Cache)**：自研 Exact Match、降噪過濾與向量比對三層架構，攔截短句按鈕點擊，達成 `~50ms` 零成本秒回。
3. **🧠 代理人記憶機制 (Agentic Memory)**：背景自動分析用戶對話，萃取用戶特徵（如年齡、居住地）寫入長期記憶庫，達成具備上下文脈絡的個性化回答。
4. **🛡️ 推理洩漏防禦 (Reasoning Guard)**：專為思考型模型 (Reasoning LLMs) 設計，透過嚴格的 Regex 剝離未閉合標籤與 Fallback 阻斷，確保思考鏈不外洩。
5. **🔍 混合式 RAG 檢索**：完美融合 Gemini Embedding 2 向量檢索與 FTS 全文檢索，透過 RRF 演算法混合排名。

👉 **[詳細核心技術與架構白皮書請點此閱讀](docs/technical_architecture.md)**

---

## 最新架構優化：本地端 M3E 中文 Embedding 轉移與安全預載 (2026-07-26 更新)

系統已將原本依賴外部 API 的 Gemini Embedding 模組，全面重構為 **本地端載入的 `moka-ai/m3e-base` 模型**（原生 768 維度），實現 100% 私有化部署與超低延遲檢索：

1. **單例與執行緒安全保護 (`EmbeddingModelSingleton`)**：
   採用雙重鎖（Async Lock + Thread Lock）與 `asyncio.to_thread` 進行 PyTorch CPU 推理，徹底解耦非同步 API 與背景同步任務，絕不阻塞 FastAPI 的 Event Loop，並防止多執行緒重複載入導致的 OOM 記憶體崩潰。
2. **FastAPI 異步預載入 (`preload_async`)**：
   於伺服器啟動時（`startup` 事件）同步等待模型載入完成，解決「首個請求卡頓」造成的 LINE Webhook 超時重試問題。
3. **無 subprocess 之背景向量同步**：
   重構 `sync_embeddings.py` 為本機引用模組，取消過去經由 `subprocess.run` 開啟子行程的運作機制，大幅節省系統記憶體開銷。
4. **寫入安全與資料保護**：
   重構智慧切塊與向量同步流程，改採「先寫入新碎塊、成功後才刪除舊碎塊」的事務順序，防止網路瞬斷導致知識庫文章遺失。

### 核心系統架構圖 (System Architecture)

```mermaid
graph TD
    User([LINE 用戶]) -->|傳送文字訊息| Webhook[FastAPI Webhook /callback]
    Webhook -->|秒回 200 OK| Webhook
    Webhook -.->|觸發 Background Task| Orchestrator[多代理人協調者 handle_text_event]
    
    subgraph Multi-Agent Orchestration
        Orchestrator --> Router[Router Agent 意圖分流]
        Router -->|快取命中| Cache[(Semantic Cache DB)]
        Router -->|需要知識庫| Expander[Query Expansion Agent]
        Expander -->|重寫 Query + Tags| Searcher[RAG QA Agent]
    end
    
    subgraph Supabase PostgreSQL
        Searcher -->|呼叫 RPC| HybridSearch{match_knowledge_hybrid}
        HybridSearch -->|Vector 檢索| pgvector[(HNSW 向量索引)]
        HybridSearch -->|Fuzzy 檢索| pgtrgm[(Trigram 模糊索引)]
        pgvector --> RRF[RRF 混合排名]
        pgtrgm --> RRF
    end
    
    RRF --> Searcher
    Searcher --> LocalLLM((本地 LLM llama.cpp))
    LocalLLM -->|生成 Flex 卡片/文字| ReplyAPI[LINE Reply/Push API]
    ReplyAPI --> User
    
    subgraph Data Pipeline & Local Embedding
        Admin[管理後台 新增文稿] --> Sync[sync_embeddings.py]
        Sync -->|1. 語意智慧切塊 smart_chunk| Chunks
        Sync -->|2. In-Process 向量推理| LocalEmbedding[moka-ai/m3e-base (PyTorch)]
        LocalEmbedding -->|寫入 768維 向量| pgvector
    end
```

---

## 完整說明手冊

更詳細的系統部署流程、管理員手冊、跨伺服器移植指南、系統微服務管理（Systemd）以及 AI 相關機制，請參閱本專案內置的完整手冊：

👉 **[AI 應用系統部署與管理手冊](output/documents/ai_system_manual.md)**
👉 **[AI 知識條目蒐集架構與操作手冊](output/documents/line_bot_admin_knowledge_manual.md)**
👉 **[LINE Bot & RAG 核心技術與架構白皮書](docs/technical_architecture.md)**

---

## 專案目錄結構

```text
├── admin/                  # 後台管理系統 (Flask 應用與模板)
├── line_bot/               # LINE Bot 核心服務與資料庫 Schema
├── models/                 # 放置 .gguf 大模型檔案之目錄 (已忽略)
├── output/                 # 打包備份與文檔輸出目錄
│   └── documents/          # 存放說明手冊之目錄
│       ├── ai_system_manual.md
│       └── line_bot_admin_knowledge_manual.md
├── backup_env.sh           # 專案與設定檔打包備份腳本
├── install_services.sh     # 註冊微服務為 Systemd 服務之腳本
├── select_model.sh         # 互動式更換模型腳本
├── start_system.sh         # 全域服務啟動腳本
├── .gitignore              # Git 忽略設定檔案
└── README.md               # 本說明文件
```
