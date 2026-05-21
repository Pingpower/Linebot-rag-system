# LINE Bot & 後台管理系統 (以本地大模型與 RAG 驅動)

本專案是一個整合了本地大語言模型（LLM）與檢索增強生成（RAG）技術的 LINE Bot 客服與智慧後台管理系統。

## 系統核心架構

- **LINE Bot (`line_bot/`)**：基於 Flask 框架開發，採用本地中文字元分詞與二元 N-Gram 比對算法實現模糊檢索。檢索完成後呼叫本地部署的大模型 API 生成人性化的解答。
- **後台控制台 (`admin/`)**：提供視覺化的 Web 管理面板（Port 8888）。具備合作企業管理、自訂參數配置、知識庫管理與 AI-Collect 自動化網頁知識萃取等功能。
- **本地推論引擎 (llama-server)**：透過 `llama.cpp` 載入 GGUF 格式的模型檔案，並藉由 CUDA 進行顯示卡加速推論。

---

## 最新功能亮點 (2026-05-21 更新)

1. **混合式原生 Flex Message 渲染 (Silent Card / Carousel 輪播)**：
   - 系統現在完美相容 LINE 官方原生 Flex JSON。當 `[FLEX_CARD]` 標籤中包含 `"type": "bubble"` 或 `"type": "carousel"` 時，自動解析渲染，支援精巧的 Silent Card 與多張輪播橫滑卡片。
2. **引導式對話 3 層限制**：
   - 針對長篇大論的業務解答進行智慧分流。AI 在前兩層提供分類引導按鈕，第三層（終點）則強制輸出最詳盡的完整解答，防止文字過長被截斷，同時避免對話層級拖沓。
3. **模型分級參數自動調配**：
   - 管理後台在熱切換模型時，會自動依據模型大小（Tiny、Medium、Large）調整 `llama-server` 啟動參數（GPU 載入層數 `gpu_layers` 與 `ctx_size`），避免 6GB 顯卡 (GTX 1060) 顯存溢出，同時最大化加速小模型。
4. **對話歷史乾淨化存檔**：
   - 儲存到 Supabase 的歷史紀錄會自動過濾 `[FLEX_CARD]` 內部 JSON 與所有 Markdown `**` 等符號，確保後台對話紀錄的純文字清晰易讀。

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

## 完整說明手冊

更詳細的系統部署流程、跨伺服器移植指南、系統微服務管理（Systemd）以及 AI 回答延遲原因的深度分析，請參閱本專案內置的完整手冊：

👉 **[AI 應用系統部署與管理手冊](output/documents/ai_system_manual.md)**

---

## 專案目錄結構

```text
├── admin/                  # 後台管理系統 (Flask 應用與模板)
├── line_bot/               # LINE Bot 核心服務與資料庫 Schema
├── models/                 # 放置 .gguf 大模型檔案之目錄 (已忽略)
├── output/                 # 打包備份與文檔輸出目錄
│   └── documents/          # 存放 ai_system_manual.md 說明手冊
├── backup_env.sh           # 專案與設定檔打包備份腳本
├── install_services.sh     # 註冊微服務為 Systemd 服務之腳本
├── select_model.sh         # 互動式更換模型腳本
├── start_system.sh         # 全域服務啟動腳本
├── .gitignore              # Git 忽略設定檔案
└── README.md               # 本說明文件
```
