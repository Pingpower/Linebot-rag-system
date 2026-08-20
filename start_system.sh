#!/bin/bash

# ==========================================
# LINE Bot 穩定化啟動與守護腳本 (Watchdog)
# ==========================================

# 基礎路徑設定
WORKSPACE_DIR="$HOME/文件"
MODELS_DIR="$WORKSPACE_DIR/models"
LLAMA_SERVER="$WORKSPACE_DIR/llama.cpp/build/bin/llama-server"
PYTHON_APP_DIR="$WORKSPACE_DIR/line_bot"
CLOUDFLARED="$WORKSPACE_DIR/cloudflared"

# 通道設定 (填寫您建立的隧道名稱)
TUNNEL_NAME="line-bot"

# 確保所有依賴檔案存在
if [ ! -x "$LLAMA_SERVER" ]; then echo "找不到 llama-server"; exit 1; fi
if [ ! -d "$PYTHON_APP_DIR" ]; then echo "找不到 Python 專案目錄"; exit 1; fi
if [ ! -x "$CLOUDFLARED" ]; then echo "找不到 cloudflared"; exit 1; fi

# ==========================================
# 模型管理選單
# ==========================================
manage_models() {
    while true; do
        # 每次進入選單都重新掃描，確保刪除後清單即時更新
        shopt -s nullglob
        MODELS=("$MODELS_DIR"/*.gguf)

        echo ""
        echo -e "\e[36m==================================================\e[0m"
        echo -e "\e[1;33m          模型管理選單 (Model Manager)          \e[0m"
        echo -e "\e[36m==================================================\e[0m"
        echo ""

        if [ ${#MODELS[@]} -eq 0 ]; then
            echo -e "\e[1;31m  找不到任何 .gguf 模型！請先下載模型。\e[0m"
            exit 1
        fi

        for i in "${!MODELS[@]}"; do
            SIZE=$(du -sh "${MODELS[$i]}" 2>/dev/null | cut -f1)
            echo -e "  \e[1;32m[$((i+1))]\e[0m $(basename "${MODELS[$i]}") \e[2m(${SIZE})\e[0m"
        done

        echo ""
        echo -e "  \e[1;31m[d]\e[0m 刪除模型"
        echo -e "  \e[1;32m[數字]\e[0m 啟動選定模型"
        echo ""
        
        if ! read -t 10 -p "請輸入選擇: " choice; then
            echo ""
            echo -e "\e[1;33m[系統] 逾時未輸入，自動載入預設模型...\e[0m"
            if [ -f "$MODELS_DIR/gemma-4-12b-it-qat-q4_0.gguf" ]; then
                SELECTED_MODEL="$MODELS_DIR/gemma-4-12b-it-qat-q4_0.gguf"
            else
                SELECTED_MODEL="${MODELS[0]}"
            fi
            echo -e "\n載入模型: \e[1;36m$(basename "$SELECTED_MODEL")\e[0m\n"
            break
        fi

        # 刪除模式
        if [[ "$choice" == "d" || "$choice" == "D" ]]; then
            echo ""
            echo -e "\e[1;31m⚠️  警告：刪除後無法復原！請選擇要刪除的模型：\e[0m"
            for i in "${!MODELS[@]}"; do
                SIZE=$(du -sh "${MODELS[$i]}" 2>/dev/null | cut -f1)
                echo -e "  \e[1;31m[$((i+1))]\e[0m $(basename "${MODELS[$i]}") \e[2m(${SIZE})\e[0m"
            done
            echo -e "  \e[2m[0] 取消，返回選單\e[0m"
            echo ""
            read -p "請輸入要刪除的編號: " del_choice

            if [[ "$del_choice" == "0" ]]; then
                echo "取消刪除。"
                continue
            fi

            if ! [[ "$del_choice" =~ ^[0-9]+$ ]] || [ "$del_choice" -lt 1 ] || [ "$del_choice" -gt "${#MODELS[@]}" ]; then
                echo -e "\e[1;31m無效編號，返回選單。\e[0m"
                continue
            fi

            TARGET="${MODELS[$((del_choice-1))]}"
            TARGET_NAME=$(basename "$TARGET")
            read -p "確定要刪除 「${TARGET_NAME}」 嗎？(y/N): " confirm

            if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
                rm -f "$TARGET"
                echo -e "\e[1;32m✅ 已成功刪除：${TARGET_NAME}\e[0m"
                sleep 1
            else
                echo "取消刪除。"
            fi
            continue
        fi

        # 啟動模式
        if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#MODELS[@]}" ]; then
            echo -e "\e[1;31m無效選擇，請重新輸入。\e[0m"
            continue
        fi

        SELECTED_MODEL="${MODELS[$((choice-1))]}"
        echo -e "\n載入模型: \e[1;36m$(basename "$SELECTED_MODEL")\e[0m\n"
        break
    done
}

# 執行模型管理選單
manage_models

# ==========================================
# 守護進程 (Watchdog) 邏輯
# ==========================================
# 這些變數用來儲存背景程序的 PID
PID_LLAMA=""
PID_FLASK=""
PID_TUNNEL=""

# 啟動前先強制清理殘留的殭屍進程，避免 Port 佔用
echo -e "\e[1;33m[系統] 正在清理舊的連線與殘留進程...\e[0m"
pkill -f "llama-server" 2>/dev/null
pkill -f "python3 app.py" 2>/dev/null
pkill -f "python3 admin/app.py" 2>/dev/null
pkill -f "cloudflared tunnel" 2>/dev/null
sleep 2

# 優雅關閉函數 (Trap Ctrl+C)
cleanup() {
    echo -e "\n\e[1;31m[系統] 收到中斷訊號，正在關閉所有服務...\e[0m"
    [ -n "$PID_LLAMA" ] && kill $PID_LLAMA 2>/dev/null
    [ -n "$PID_FLASK" ] && kill $PID_FLASK 2>/dev/null
    [ -n "$PID_ADMIN" ] && kill $PID_ADMIN 2>/dev/null
    [ -n "$PID_TUNNEL" ] && kill $PID_TUNNEL 2>/dev/null
    pkill -P $$ # 確保所有子進程都被殺死
    echo -e "\e[1;32m[系統] 服務已安全關閉。\e[0m"
    exit 0
}
trap cleanup SIGINT SIGTERM

# 監控 Llama-server
watchdog_llama() {
    while true; do
        echo -e "\e[1;34m[Watchdog] 啟動 Llama-server...\e[0m"
        $LLAMA_SERVER -m "$SELECTED_MODEL" --host 0.0.0.0 --port 8080 --ctx-size 8192 --reasoning off --n-gpu-layers 99 --threads 6 --threads-batch 6 --parallel 2 --cache-type-k q8_0 --cache-type-v q8_0 --no-mmap --mlock --flash-attn on > "$WORKSPACE_DIR/llama.log" 2>&1 &
        PID_LLAMA=$!
        wait $PID_LLAMA
        echo -e "\e[1;31m[Watchdog] Llama-server 異常關閉，3秒後重啟...\e[0m"
        sleep 3
    done
}

# 監控 Python Flask (LINE Bridge)
watchdog_flask() {
    cd "$PYTHON_APP_DIR"
    export PYTHONPATH="$HOME/.local/lib/python3.12/site-packages:$PYTHONPATH"
    while true; do
        echo -e "\e[1;35m[Watchdog] 啟動 Python Flask...\e[0m"
        /usr/bin/python3 app.py > "$WORKSPACE_DIR/flask.log" 2>&1 &
        PID_FLASK=$!
        wait $PID_FLASK
        echo -e "\e[1;31m[Watchdog] Python Flask 異常關閉，3秒後重啟...\e[0m"
        sleep 3
    done
}

# 監控 Cloudflared (Fixed Tunnel)
watchdog_tunnel() {
    while true; do
        echo -e "\e[1;36m[Watchdog] 啟動 Cloudflare Tunnel ($TUNNEL_NAME)...\e[0m"
        # 使用我們建立的 config.yml 進行路由
        $CLOUDFLARED tunnel run $TUNNEL_NAME > "$WORKSPACE_DIR/cloudflared.log" 2>&1 &
        PID_TUNNEL=$!
        wait $PID_TUNNEL
        echo -e "\e[1;31m[Watchdog] Cloudflare Tunnel 異常關閉，3秒後重啟...\e[0m"
        sleep 3
    done
}

watchdog_admin() {
    while true; do
        echo -e "\e[1;35m[Watchdog] 啟動管理後台 (port 8888)...\e[0m"
        cd "$WORKSPACE_DIR"
        python3 admin/app.py > "$WORKSPACE_DIR/admin.log" 2>&1 &
        PID_ADMIN=$!
        wait $PID_ADMIN
        echo -e "\e[1;31m[Watchdog] 管理後台異常關閉，3秒後重啟...\e[0m"
        sleep 3
    done
}


watchdog_llama &
watchdog_flask &
watchdog_admin &
sleep 2 # 稍微等候伺服器啟動
watchdog_tunnel &

echo -e "\n\e[1;32m[系統] 所有服務已進入背景守護模式！\e[0m"
echo -e "您可以查看日誌："
echo -e "  tail -f $WORKSPACE_DIR/llama.log
  tail -f $WORKSPACE_DIR/flask.log
  tail -f $WORKSPACE_DIR/admin.log
  tail -f $WORKSPACE_DIR/cloudflared.log
"
echo -e "\n\e[1;33m按下 Ctrl+C 即可安全關閉所有服務。\e[0m\n"

# 讓主程式等待，這樣 trap 才會持續生效
wait
