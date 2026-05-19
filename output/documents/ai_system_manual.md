# AI 模型應用、環境優化與 LINE BOT 後台管理系統手冊

本手冊旨在提供一套完整的指南，說明如何建立、優化本地端 AI 模型應用，進行 Antigravity 開發環境的建置與防洪優化，以及操作 LINE Bot 的 AI 應用與後台管理系統。手冊內容已針對當前工作區的實際腳本、架構與 Supabase/RAG 整合進行了整合與提煉。

---

## 1. 本地端 AI 模型應用建立、優化與跨機複製指南

### 1.1 系統架構簡介

本系統採用本機大語言模型 (Local LLM) 作為核心推理引擎，前端藉由 Flask LINE Bot 與使用者互動，後端則以 Flask Admin Panel 提供管理介面，並採用 Supabase 作為雲端資料庫（儲存公司資訊、知識庫條目及對話歷史）。外部流量則透過 Cloudflare Tunnel 安全地導入本地運行的 Flask 服務中，實現無須公網 IP 的外部對接。

### 1.2 本地 LLM (llama.cpp) 編譯與模型部署

本系統使用 `llama.cpp` 作為模型推理後端。若要在新電腦部署，請依循以下步驟編譯與配置：

1. **複製與編譯 `llama.cpp`**

   ```bash
   git clone https://github.com/ggerganov/llama.cpp.git ~/文件/llama.cpp
   cd ~/文件/llama.cpp

   # 若有 NVIDIA GPU（支援 CUDA 加速推理）：
   cmake -B build -DGGML_CUDA=ON
   cmake --build build --config Release -j$(nproc)

   # 若僅使用 CPU：
   cmake -B build
   cmake --build build --config Release -j$(nproc)
   ```

   編譯完成後，執行檔 `llama-server` 將位於 `~/文件/llama.cpp/build/bin/llama-server`。

2. **建立模型目錄與下載模型**
   本系統的模型存放於 `~/文件/models`。請在此目錄放置 `.gguf` 格式的量化模型，例如：
   - `Qwen2.5-7B-Instruct-Q4_K_M.gguf`
   - `NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-UD-Q4_K_M.gguf`

### 1.3 Python 虛擬環境與套件依賴安裝

系統的 LINE Bot 與 Admin 後台皆由 Python 3.12 驅動，請於目標電腦建置 Python 環境：

1. **建立並啟用虛擬環境**

   ```bash
   cd ~/文件
   python3 -m venv venv
   source venv/bin/activate
   ```

2. **安裝核心依賴套件**

   ```bash
   pip install --upgrade pip
   pip install flask openai supabase line-bot-sdk python-dotenv requests beautifulsoup4
   ```

### 1.4 使用備份腳本進行跨機複製與部署還原

專案附帶了 `backup_env.sh` 與 `install_services.sh` 腳本，用以將整套環境打包並於別台電腦快速重建。

#### 1.4.1 環境打包 (來源機)

在來源機工作區直接執行備份腳本：

```bash
bash ~/文件/backup_env.sh
```

**腳本行為說明：**

- 自動建立備份目錄，並將 `~/.gemini/antigravity/mcp_config.json` 複製進來。
- 打包工作區下的重要啟動與管理腳本 (`backup_env.sh`, `start_system.sh`, `install_services.sh`, `select_model.sh` 等)。
- 以 `rsync` 排除快取檔與 `.env` 後，打包 `line_bot` 與 `admin` 目錄。
- 備份現有的 Systemd user 服務檔。
- 將上述內容壓縮為 `~/文件/output/antigravity_backup_YYYYMMDD_HHMMSS.tar.gz`。

#### 1.4.2 部署還原 (目標機)

1. **傳輸並解壓縮：**
   將 `.tar.gz` 壓縮檔傳輸至目標機的目標目錄並解壓縮。

   ```bash
   # 以解壓到 ~/my_project 為例
   mkdir -p ~/my_project
   tar -xzf antigravity_backup_*.tar.gz -C ~/my_project/
   ```

2. **重設環境變數：**
   在 `line_bot/` 底下重新建立 `.env` 檔案，填入該環境 the LINE 與 Supabase 連線憑證：

   ```ini
   LINE_CHANNEL_SECRET=your_line_channel_secret
   LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token
   SUPABASE_URL=https://your-project-id.supabase.co
   SUPABASE_SERVICE_KEY=your_supabase_service_role_key
   ```

3. **放置 AI 模型：**
   將 `.gguf` 模型檔放置於目標機的 `models/` 底下。
4. **安裝與註冊 Systemd 服務：**
   執行工作區的安裝指令。

#### 1.4.3 跨伺服器路徑適應與環境變數警示

由於目標伺服器的使用者名稱與目錄名稱不見得與來源伺服器一致（例如可能使用 `/opt/line_bot` 或 `/home/ubuntu/Documents`，而非預設的 `/home/pipadmin/文件`），請在還原與執行腳本前注意以下設定：

1. **修改腳本中的 `WORKSPACE` 路徑：**
   請打開 `select_model.sh`、`install_services.sh` 與 `start_system.sh` 等腳本檔案，將其頂部的：

   ```bash
   WORKSPACE="$HOME/文件"
   ```

   修改為目標伺服器上的實際專案根路徑。例如：

   ```bash
   WORKSPACE="/home/ubuntu/my_project"
   ```

2. **確認 Systemd 服務檔中的路徑設定：**
   在執行 `bash install_services.sh` 前，安裝程式會動態取得當下的 `WORKSPACE` 值並將其寫入 Systemd user 服務設定中。若您是在還原後才移動資料夾，需手動修正 `~/.config/systemd/user/` 目錄下各服務檔（如 `linebot-flask.service`）的 `WorkingDirectory` 與 `ExecStart` 絕對路徑，並執行 `systemctl --user daemon-reload` 重載配置。

### 1.5 LLaMA Server 參數微調與 GPU/CPU 效能優化

模型伺服器 `llama-server` 的啟動設定記錄於 `linebot-llama.service` 中，我們可以根據硬體資源優化下列參數：

- `--ctx-size 8192`：調整上下文長度限制（記憶體不足時可下調至 `4096` 或 `2048`）。
- `--n-gpu-layers 15`：指定加載到 GPU 上的神經網路層數。若 GPU 記憶體較小或在純 CPU 環境，可將此值設為 `0`；若顯存足夠，可設為 `99` (全 GPU 加載)，能顯著增快推理速度。
- `--threads 8`：使用的 CPU 線程數。建議設定為實體 CPU 核心數 (例如 `nproc` 的一半到 80%)，過多執行緒會造成執行緒競爭而拖慢效能。
- `--parallel 2`：平行請求處理數。可允許同時為多少個 LINE 使用者進行生成，通常在本地端設為 `1` 或 `2` 以確保單次產出流暢度。

### 1.6 互動式模型切換與更換操作指南

當您需要為系統更換或更新大語言模型時，可以透過工作區提供的 `select_model.sh` 進行安全且自動化的模型更換。

#### 1.6.1 切換模型的指令與流程

在終端機中執行：

```bash
bash ~/my_project/select_model.sh
```

**操作步驟：**

1. 腳本會自動掃描您在 `WORKSPACE/models/` 目錄下存放的所有 `.gguf` 檔案。
2. 畫面會列出可用模型列表、檔案大小，以及當前正啟用的模型標記。
3. 依照提示輸入對應的編號並按 Enter 鍵確認。
4. 腳本會自動覆寫 `systemd` 服務配置檔中的模型路徑，並寫入選取紀錄至 `~/.config/linebot/selected_model`。
5. 腳本會自動呼叫 `systemctl --user daemon-reload` 與 `systemctl --user restart linebot-llama` 重啟模型服務。

#### 1.6.2 驗證更換狀態

切換模型後，請執行以下命令以確認新模型是否成功載入：

```bash
# 檢查 LLaMA 服務狀態
systemctl --user status linebot-llama

# 監控模型載入日誌（特別注意是否有顯存溢出或記憶體不足錯誤）
tail -n 50 ~/my_project/llama.log
```

---

## 2. Antigravity 開發環境建置與優化規則

為了在使用 Antigravity 這款強大的 AI 編碼助理時，取得最高的運行效率與最少的 Token 損耗，系統建立了嚴格的開發防洪機制。

### 2.1 Antigravity 工具與 Context-Mode 精髓

在開發過程中，必須避免將過大的命令輸出或檔案內容「淹沒」至 context 視窗中。

- **原則：** 讀取程式碼以進行「修改」時，使用 `view_file` 與 `replace_file_content` 屬正常操作。但若是為了進行「分析、統計、搜尋與程式碼結構探索」，則禁止直接讀取大檔案至上下文。
- **對策：** 善用 `context-mode` 提供的沙盒工具。

### 2.2 開發防洪與 Token 節約原則 (Think in Code)

當需要對專案或記錄檔進行資料處理與查詢時，落實 「Think in Code」 原則：

- **實作做法：** 撰寫一段 Node.js (JavaScript) 或 Python 腳本，透過 `mcp_context-mode_ctx_execute` 傳入沙盒中執行。讓程式在沙盒中解析並計算結果，最後只以 `console.log()` 印出最終精簡答案。
- **錯誤示範：** 在終端機執行 `cat flask.log` 或 `grep -rn "pattern" ./` 產生上百行文字，導致 Token 視窗瞬間額滿、上下文崩塌。
- **正確示範：** 在沙盒中寫入分析指令，最後只印出精簡後的分析結果。

```javascript
// 透過 ctx_execute 執行
const fs = require("fs");
const logs = fs.readFileSync("flask.log", "utf-8");
// ... 在此進行程式分析與篩選 ...
console.log(`總錯誤數: ${errorCount}`); // 只將這一行帶回給 Antigravity
```

### 2.3 團隊開發協作規範 (GEMINI.md 與 AGENTS.md)

工作區中有兩份核心規範檔：

- **`GEMINI.md`：** 記錄了 Model Context Protocol (MCP) 路由規則，特別是 `run_command` 的限制：`run_command` 僅能用於 `git`、`mkdir`、`rm`、`mv`、`cd`、`ls`、`npm install`、`pip install` 等基本系統操作。任何可能產生大輸出的指令都必須進入 `ctx_execute` 沙盒中執行。
- **`AGENTS.md`：** 規範秘書工作模式（Secretary Mode），所有設計圖、Markdown 手冊、工具腳本等，產出後一律須儲存至特定目錄：
  - 手冊/文件：`~/文件/output/documents/`
  - 設計/圖片：`~/文件/output/designs/`
  - 系統工具/腳本：`~/文件/output/tools/`
- 所有 AI 發出的 commit，其 commit message 必須包含 `Co-Authored-By: Antigravity <noreply@antigravity.dev>`。

---

## 3. LINE BOT AI 應用與後台管理手冊

### 3.1 LINE Bot 的 Local RAG 檢索與 LLM 回答機制

LINE Bot (位於 `line_bot/app.py`) 整合了檢索增強生成 (RAG) 機制。當使用者發送訊息時，後端會執行以下流程：

1. **驗證與接收：** 接收來自 LINE 平台的 Webhook 請求，並透過 `LINE_CHANNEL_SECRET` 進行簽章驗證。
2. **知識庫檢索：** 調用 `search_knowledge(company_id, user_query)` 尋找 Supabase 中與使用者問題相關的知識條目。
3. **載入公司資產：** 撈取該公司所設定的資產與變數（如官方網站、活動資訊）。
4. **構建 Prompt：** 將檢索到的知識、公司資產與使用者的歷史對話合併，注入系統 Prompt 中。
5. **本地 LLM 推理：** 調用 `llm_client` (對接 `http://127.0.0.1:8080/v1`)，在本地端取得回應，並將生成的內容透過 LINE Bot SDK 回覆給使用者。

#### 3.1.2 為什麼已經有資料庫檢索，AI 回答仍需要等待較長時間？

在 LINE Bot 與使用者的對談中，常會遇到「明明已經用資料庫搜尋，卻依然要等待數秒甚至更久才收到回覆」的現象。其技術原因與優化對策如下：

1. **檢索與生成是兩個完全不同的階段**
   - **資料庫檢索（快速）：** Supabase 查詢與 Python 的本地中文字元模糊檢索，只需消耗幾毫秒到幾十毫秒即可完成。它僅負責「找出適合的參考資料」。
   - **大語言模型推理與回答生成（緩慢）：** 當取得參考資料後，系統必須將其與對話歷史一起打包，交給本地運行的 LLM 進行數學矩陣計算並逐字產生答覆。這是主要的效能瓶頸。

2. **本地推理硬體與算力瓶頸**
   - 本地模型的推理速度（每秒產生的 Token 數）完全取決於硬體（特別是顯示卡顯存與記憶體頻寬）。
   - 如果運算未完全載入 GPU（`--n-gpu-layers` 設定過低）而需依賴 CPU 運算，或者使用的模型參數量過大（如 30B 級別），生成速度便會降至每秒僅 1~3 個 Token。一段 150 字的回覆就可能需要等待 30 秒至 1 分鐘。

3. **LINE 平台不支援串流輸出**
   - 雖然 LLaMA 支援邊生成邊輸出的串流模式（Streaming），但 LINE 平台的訊息接收 API（Reply Message API）規定必須在一次性的 HTTP 請求中提交「完整的回答字串」。
   - 因此，後端系統必須在背景等待模型「把整句話完全說完」後，才能將結果回傳給 LINE 平台。這在感官上造成了較為明顯的延遲。

#### 3.1.3 效能優化對策

- **切換更輕量的模型：** 透過 `select_model.sh` 切換至 7B 或 3B 級別的量化模型，其推理速度通常為 30B 模型的數倍。
- **調高 GPU 加速層數：** 增加 `llama-server` 的 `--n-gpu-layers` 參數值，盡可能將更多網路層載入 GPU 顯存中運行。
- **縮短輸入上下文：** 在 `line_bot/app.py` 中限制 RAG 搜尋回傳的條目筆數（例如限制為 `limit = 1` 或 `limit = 2`），以減少 LLM 運算的首字等待時間（Time to First Token）。

### 3.2 N-Gram 中文模糊檢索算法（克服 Postgres 限制）

由於 PostgreSQL 預設的全文字檢索 (FTS) 對於無空格的中文斷詞效果極差，本系統設計了一套基於 Python 本地端處理的 **中文字元 N-Gram 分數模糊檢索算法**：

```python
def search_knowledge(company_id: str, query: str, limit: int = 3) -> list[dict]:
    # 1. 一次性自 Supabase 中撈取該公司所有的啟用知識庫條目
    res = supabase.table('knowledge_base').select('title, content').eq('company_id', company_id).eq('is_active', True).execute()
    all_docs = res.data or []

    # 2. 清理查詢語句並做中文字元分詞 (排除常用的停用詞)
    clean_query = re.sub(r'[^\w\s]', '', query)
    query_chars = [c for c in clean_query if c.strip() and c not in stop_words]

    # 3. 建立二元/多元 N-Gram
    ngrams = []
    for i in range(len(query_chars) - 1):
        ngrams.append("".join(query_chars[i:i+2]))

    # 4. 對每一個條目的標題與內容進行匹配評分
    scored_docs = []
    for doc in all_docs:
        score = 0
        text_to_search = doc['title'] * 2 + doc['content'] # 提高標題權重

        # 字元命中與 N-gram 命中加分
        for char in query_chars:
            if char in text_to_search:
                score += 1
        for ngram in ngrams:
            if ngram in text_to_search:
                score += 3

        if score > 0:
            scored_docs.append((score, doc))

    # 5. 排序並回傳評分最高的前 N 筆條目
    scored_docs.sort(key=lambda x: x[0], reverse=True)
    return [doc for score, doc in scored_docs[:limit]]
```

### 3.3 管理後台系統架構與功能模組

後台管理系統 (位於 `admin/app.py`，運作於 Port 8888) 是一個以 Flask 撰寫的視覺化 Web 控制台，主要功能模組包含：

- **登入驗證 (`/login`, `/logout`)：** 基於帳號密碼保護管理介面安全性。
- **公司與資產管理 (`/companies` 系列)：** 管理合作的企業用戶，並為其分配自訂變數/資產（如 line token、官方連結、推廣訊息等）。
- **知識庫中心 (`/knowledge` 系列)：** 提供條目的手動新增、刪除、關鍵字檢索。
- **統計面板 (`/stats`, `/api/stats/<company_id>`)：** 可視化呈現各公司 Bot 的 API 呼叫次數、Token 消耗量、使用者互動次數等趨勢。

### 3.4 知識庫智慧收集與萃取技術 (AI-Collect & LLM Extract)

手動輸入知識條目費時費力，後台管理系統整合了 **AI-Collect 智慧收集模組**：

1. **網址探測 (`/knowledge/ai-collect/detect-url`)：**
   當管理員輸入一個網址，系統會使用 `BeautifulSoup` 抓取網頁內容，探測其是否為目錄頁（Index Page），並自動分析抓取頁面中關聯的子連結，產出一個待抓取清單。
2. **網頁擷取與 AI 萃取 (`/knowledge/ai-collect` 與 `ai-save`)：**
   對選定的子連結進行正文擷取，去除 HTML 標籤後，將純文字傳送至 `_llm_extract` 方法。
3. **本地 LLM 條目化提取 (`_llm_extract`)：**
   透過高度約束的 Prompt，要求本地 LLM (llama-server) 分析原始長文，並將內容摘要、重組為 2-3 個符合 JSON 規格的知識條目（包含 `title`、`content` 與 `tags`）。
4. **寫入資料庫：**
   管理員在後台預覽並微調 AI 萃取出的條目後，一鍵儲存至 Supabase，即可立刻在 LINE Bot 中被 RAG 檢索使用，實現知識的自動化擴充。

### 3.5 系統服務管理 (Systemd --user 四大服務)與維護調測

本系統部署於 Ubuntu 時，全部註冊在系統的 User 空間 (`systemd --user`) 下運行，無須 root 權限，非常利於普通用戶權限的管理。

#### 3.5.1 四大服務管理命令

- **LLaMA Server 服務 (`linebot-llama`)**
  - 啟動：`systemctl --user start linebot-llama`
  - 停止：`systemctl --user stop linebot-llama`
  - 狀態：`systemctl --user status linebot-llama`
- **LINE Bot Flask 服務 (`linebot-flask`)**
  - 啟動：`systemctl --user start linebot-flask`
  - 停止：`systemctl --user stop linebot-flask`
- **後台管理服務 (`linebot-admin`)**
  - 啟動：`systemctl --user start linebot-admin`
  - 停止：`systemctl --user stop linebot-admin`
- **Cloudflare Tunnel 服務 (`linebot-tunnel`)**
  - 啟動：`systemctl --user start linebot-tunnel`
  - 停止：`systemctl --user stop linebot-tunnel`

#### 3.5.2 全域啟動與日誌監控

- 工作區提供了 `start_system.sh` 用於一鍵重啟所有子系統並以 Watchdog 模式守護。
- **即時日誌監控方式：**
  - LLaMA 運作日誌：`tail -f ~/my_project/llama.log`
  - LINE Bot 錯誤日誌：`tail -f ~/my_project/flask.log`
  - 後台系統日誌：`tail -f ~/my_project/admin.log`
  - 穿透隧道日誌：`tail -f ~/my_project/cloudflared.log`
