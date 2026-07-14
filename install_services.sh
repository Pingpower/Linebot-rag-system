#!/bin/bash
# ============================================================
# install_services.sh — 安裝 LINE Bot 平台為 systemd 服務
# 執行一次即可，之後服務會開機自啟、自動重啟
# 使用方式：bash ~/文件/install_services.sh
# ============================================================
set -e

WORKSPACE="$HOME/文件"
USER_SYSTEMD="$HOME/.config/systemd/user"
PYTHON=$(which python3)

echo "=== LINE Bot 服務安裝程式 ==="
mkdir -p "$USER_SYSTEMD"
mkdir -p "$HOME/.config/linebot"

# ── 1. 讀取現有選擇的模型（若有）──────────────────────────
MODEL_CONFIG="$HOME/.config/linebot/selected_model"
if [ -f "$MODEL_CONFIG" ]; then
    CURRENT_MODEL=$(cat "$MODEL_CONFIG")
    echo "目前模型：$CURRENT_MODEL"
else
    # 自動找第一個 .gguf
    CURRENT_MODEL=$(find "$WORKSPACE/models" -name "*.gguf" | sort | head -1)
    if [ -z "$CURRENT_MODEL" ]; then
        echo "❌ 找不到任何 .gguf 模型，請先下載模型"
        exit 1
    fi
    echo "$CURRENT_MODEL" > "$MODEL_CONFIG"
    echo "自動選擇模型：$CURRENT_MODEL"
fi

LLAMA_BIN="$WORKSPACE/llama.cpp/build/bin/llama-server"

# ── 2. llama-server 服務 ────────────────────────────────────
cat > "$USER_SYSTEMD/linebot-llama.service" << EOF
[Unit]
Description=LINE Bot LLaMA Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKSPACE
ExecStart=$LLAMA_BIN --model $(cat $MODEL_CONFIG) --host 127.0.0.1 --port 8080 --ctx-size 4096 --n-gpu-layers 99 --threads 6 --threads-batch 6 --parallel 2 --cache-type-k q8_0 --cache-type-v q8_0 --no-mmap --mlock --flash-attn --log-disable
Restart=always
RestartSec=5
StandardOutput=append:$WORKSPACE/llama.log
StandardError=append:$WORKSPACE/llama.log
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF

# ── 3. Flask LINE Bot 服務 ─────────────────────────────────
cat > "$USER_SYSTEMD/linebot-flask.service" << EOF
[Unit]
Description=LINE Bot Flask App (port 5000)
After=network.target linebot-llama.service

[Service]
Type=simple
WorkingDirectory=$WORKSPACE/line_bot
ExecStart=$PYTHON $WORKSPACE/line_bot/app.py
Restart=always
RestartSec=5
StandardOutput=append:$WORKSPACE/flask.log
StandardError=append:$WORKSPACE/flask.log
Environment=HOME=$HOME
EnvironmentFile=$WORKSPACE/line_bot/.env

[Install]
WantedBy=default.target
EOF

# ── 4. 管理後台服務 ────────────────────────────────────────
cat > "$USER_SYSTEMD/linebot-admin.service" << EOF
[Unit]
Description=LINE Bot Admin Panel (port 8888)
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKSPACE
ExecStart=$PYTHON $WORKSPACE/admin/app.py
Restart=always
RestartSec=5
StandardOutput=append:$WORKSPACE/admin.log
StandardError=append:$WORKSPACE/admin.log
Environment=HOME=$HOME
EnvironmentFile=$WORKSPACE/line_bot/.env

[Install]
WantedBy=default.target
EOF

# ── 5. Cloudflare Tunnel 服務 ──────────────────────────────
CLOUDFLARED=$(which cloudflared 2>/dev/null || echo "$WORKSPACE/cloudflared")
cat > "$USER_SYSTEMD/linebot-tunnel.service" << EOF
[Unit]
Description=LINE Bot Cloudflare Tunnel
After=network.target linebot-flask.service

[Service]
Type=simple
ExecStart=$CLOUDFLARED tunnel --config $HOME/.cloudflared/config.yml run
Restart=always
RestartSec=5
StandardOutput=append:$WORKSPACE/cloudflared.log
StandardError=append:$WORKSPACE/cloudflared.log
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF

# ── 6. 啟用所有服務 ─────────────────────────────────────────
echo ""
echo "=== 啟用 systemd 服務 ==="
systemctl --user daemon-reload

for svc in linebot-llama linebot-flask linebot-admin linebot-tunnel; do
    systemctl --user enable "$svc"
    echo "✅ 已啟用 $svc"
done

echo ""
echo "=== 啟動服務 ==="

# 先停掉現有的手動進程
pkill -f "llama-server" 2>/dev/null || true
pkill -f "python3 app.py" 2>/dev/null || true
pkill -f "python3 admin/app.py" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 2

for svc in linebot-llama linebot-flask linebot-admin linebot-tunnel; do
    systemctl --user start "$svc"
    echo "🚀 已啟動 $svc"
done

echo ""
echo "=========================================="
echo "✅ 安裝完成！服務現在由 systemd 管理"
echo ""
echo "常用指令："
echo "  systemctl --user status linebot-flask    # 查看 Flask 狀態"
echo "  systemctl --user status linebot-llama    # 查看 LLaMA 狀態"
echo "  systemctl --user restart linebot-flask   # 重啟 Flask"
echo "  journalctl --user -u linebot-flask -f    # 即時查看 Flask 日誌"
echo ""
echo "換模型請執行：bash ~/文件/select_model.sh"
echo "=========================================="
