#!/bin/bash
# ============================================================
# select_model.sh — 互動式模型選擇器
# 選完後自動更新設定並重啟 llama-server
# ============================================================

WORKSPACE="$HOME/文件"
MODEL_DIR="$WORKSPACE/models"
MODEL_CONFIG="$HOME/.config/linebot/selected_model"

echo "=== 模型選擇器 ==="
echo ""

# 列出所有模型
mapfile -t MODELS < <(find "$MODEL_DIR" -name "*.gguf" | sort)

if [ ${#MODELS[@]} -eq 0 ]; then
    echo "❌ 找不到任何模型（.gguf 格式）"
    echo "請將模型放在：$MODEL_DIR"
    exit 1
fi

SELECTED=""
if [ -n "$1" ]; then
    # Support passing basename or absolute path
    for m in "${MODELS[@]}"; do
        if [ "$(basename "$m")" = "$1" ] || [ "$m" = "$1" ]; then
            SELECTED="$m"
            break
        fi
    done
    if [ -z "$SELECTED" ]; then
        echo "❌ 找不到指定的模型：$1"
        exit 1
    fi
    echo "$SELECTED" > "$MODEL_CONFIG"
    echo "✅ 已選擇（非互動）：$(basename "$SELECTED")"
else
    echo "可用模型："
    for i in "${!MODELS[@]}"; do
        SIZE=$(du -sh "${MODELS[$i]}" 2>/dev/null | cut -f1)
        NAME=$(basename "${MODELS[$i]}")
        CURRENT=""
        [ -f "$MODEL_CONFIG" ] && [ "$(cat $MODEL_CONFIG)" = "${MODELS[$i]}" ] && CURRENT=" ← 目前使用"
        printf "  [%d] %-55s %s%s\n" $((i+1)) "$NAME" "$SIZE" "$CURRENT"
    done

    echo ""
    read -p "請輸入數字選擇模型（Enter = 取消）： " CHOICE

    if [ -z "$CHOICE" ]; then
        echo "已取消"
        exit 0
    fi

    INDEX=$((CHOICE - 1))
    if [ $INDEX -lt 0 ] || [ $INDEX -ge ${#MODELS[@]} ]; then
        echo "❌ 無效的選擇"
        exit 1
    fi

    SELECTED="${MODELS[$INDEX]}"
    echo "$SELECTED" > "$MODEL_CONFIG"
    echo "✅ 已選擇：$(basename "$SELECTED")"
fi

# 根據目標模型自動配置最佳硬體參數
M_NAME_LOWER=$(echo "$(basename "$SELECTED")" | tr '[:upper:]' '[:lower:]')
FILE_SIZE_BYTES=$(stat -c%s "$SELECTED" 2>/dev/null || echo 0)
FILE_SIZE_MB=$(( FILE_SIZE_BYTES / 1048576 ))

THREADS=6
GPU_LAYERS=10
CTX_SIZE=4096
PARALLEL=2

# 0. 判斷特定優化模型 Mai_Base
if [[ "$M_NAME_LOWER" =~ "mai_base" ]]; then
    GPU_LAYERS=99
    THREADS=6
    CTX_SIZE=4096
# 1. 判斷是否為極小模型 (符合關鍵字或檔案大小小於 4.8 GB 即 4915 MB)
elif [[ "$M_NAME_LOWER" =~ "gemma-4" || "$M_NAME_LOWER" =~ "4b" || "$M_NAME_LOWER" =~ "3b" || "$M_NAME_LOWER" =~ "2b" || "$M_NAME_LOWER" =~ "gemma2-2b" || "$M_NAME_LOWER" =~ "nemotron" ]] || [ "$FILE_SIZE_MB" -gt 100 ] && [ "$FILE_SIZE_MB" -lt 4915 ]; then
    GPU_LAYERS=99
    CTX_SIZE=4096
# 2. 判斷是否為中等模型
elif [[ "$M_NAME_LOWER" =~ "8b" || "$M_NAME_LOWER" =~ "7b" || "$M_NAME_LOWER" =~ "9b" || "$M_NAME_LOWER" =~ "gemma2-9b" ]]; then
    GPU_LAYERS=24
    CTX_SIZE=4096
# 3. 檔案大小 fallback 判斷 (檔案小於 7.5 GB 即 7680 MB 但剛好沒匹配到關鍵字)
elif [ "$FILE_SIZE_MB" -gt 100 ] && [ "$FILE_SIZE_MB" -lt 7680 ]; then
    GPU_LAYERS=20
    CTX_SIZE=4096
fi

# 額外優化參數對齊 app.py
EXTRA_ARGS=()
if [[ "$M_NAME_LOWER" =~ "moe" || "$M_NAME_LOWER" =~ "a3b" || "$M_NAME_LOWER" =~ "mixtral" || "$M_NAME_LOWER" =~ "dbrx" ]]; then
    EXTRA_ARGS+=("--cpu-moe")
fi
EXTRA_ARGS+=("--no-mmap")
EXTRA_ARGS+=("--mlock")
EXTRA_ARGS+=("--flash-attn")
EXTRA_ARGS+=("--cache-type-k q8_0")
EXTRA_ARGS+=("--cache-type-v q8_0")
EXTRA_STR="${EXTRA_ARGS[*]}"

# 更新 systemd 服務 ExecStart 的模型路徑與優化參數
USER_SYSTEMD="$HOME/.config/systemd/user"
LLAMA_BIN="$WORKSPACE/llama.cpp/build/bin/llama-server"

cat > "$USER_SYSTEMD/linebot-llama.service" << EOF
[Unit]
Description=LINE Bot LLaMA Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKSPACE
ExecStart=$LLAMA_BIN --model $SELECTED --host 127.0.0.1 --port 8080 --ctx-size $CTX_SIZE --n-gpu-layers $GPU_LAYERS --threads $THREADS --threads-batch $THREADS --parallel $PARALLEL $EXTRA_STR --log-disable
Restart=always
RestartSec=10
StandardOutput=append:$WORKSPACE/llama.log
StandardError=append:$WORKSPACE/llama.log
Environment=HOME=$HOME
LimitMEMLOCK=infinity

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload

echo "🔄 正在停止舊的 llama-server..."
systemctl --user stop linebot-llama 2>/dev/null || true
pkill -9 -f "llama-server" 2>/dev/null || true
sleep 2

echo "🔄 正在啟動新模型..."
systemctl --user start linebot-llama

sleep 3
STATUS=$(systemctl --user is-active linebot-llama)
if [ "$STATUS" = "active" ]; then
    echo "✅ llama-server 已用新模型啟動"
else
    echo "⚠️  啟動中，請稍後查看狀態：systemctl --user status linebot-llama"
fi
