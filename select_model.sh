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
echo ""
echo "✅ 已選擇：$(basename $SELECTED)"

# 更新 systemd 服務 ExecStart 的模型路徑
USER_SYSTEMD="$HOME/.config/systemd/user"
LLAMA_BIN="$WORKSPACE/llama.cpp/build/bin/llama-server"

cat > "$USER_SYSTEMD/linebot-llama.service" << EOF
[Unit]
Description=LINE Bot LLaMA Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKSPACE
ExecStart=$LLAMA_BIN \\
    --model $SELECTED \\
    --host 127.0.0.1 \\
    --port 8080 \\
    --ctx-size 8192 \\
    --n-gpu-layers 10 \\
    --threads 8 \\
    --parallel 2 \\
    --log-disable
Restart=always
RestartSec=10
StandardOutput=append:$WORKSPACE/llama.log
StandardError=append:$WORKSPACE/llama.log
Environment=HOME=$HOME

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
