#!/bin/bash
# ============================================================
# backup_env.sh — 備份 Antigravity 環境設定
# 用途：在遷移到新機器前，打包所有設定（排除 secrets 和大檔案）
# 使用方式：bash ~/文件/backup_env.sh
# ============================================================
set -e

BACKUP_DIR="$HOME/文件/output/env_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "=== Antigravity 環境備份 ==="
echo "備份目標：$BACKUP_DIR"
echo ""

# ── 1. Antigravity 設定 ──────────────────────────────────────
echo "📦 備份 Antigravity 設定..."
mkdir -p "$BACKUP_DIR/antigravity"
cp ~/.gemini/antigravity/mcp_config.json "$BACKUP_DIR/antigravity/" 2>/dev/null && echo "  ✅ mcp_config.json"

# Skills 很大（~70MB），單獨處理
echo "  ℹ️  Skills 路徑：~/.gemini/antigravity/skills/ (1400+ 個，請手動 rsync)"

# ── 2. GEMINI.md / AGENTS.md ─────────────────────────────────
echo ""
echo "📦 備份 workspace 設定..."
mkdir -p "$BACKUP_DIR/workspace"
for f in GEMINI.md AGENTS.md install_services.sh select_model.sh start_system.sh server.sh chat.sh; do
    if [ -f "$HOME/文件/$f" ]; then
        cp "$HOME/文件/$f" "$BACKUP_DIR/workspace/" && echo "  ✅ $f"
    fi
done

# ── 3. LINE Bot 程式碼（排除 .env secrets）───────────────────
echo ""
echo "📦 備份 LINE Bot 程式碼（排除 .env）..."
mkdir -p "$BACKUP_DIR/line_bot"
rsync -a --exclude='.env' --exclude='__pycache__' --exclude='*.pyc' \
    "$HOME/文件/line_bot/" "$BACKUP_DIR/line_bot/" && echo "  ✅ line_bot/"

# ── 4. Admin Panel ────────────────────────────────────────────
echo ""
echo "📦 備份 Admin Panel..."
mkdir -p "$BACKUP_DIR/admin"
rsync -a --exclude='__pycache__' --exclude='*.pyc' \
    "$HOME/文件/admin/" "$BACKUP_DIR/admin/" && echo "  ✅ admin/"

# ── 5. 目前模型選擇（不含模型本體）──────────────────────────
echo ""
echo "📦 備份模型設定..."
mkdir -p "$BACKUP_DIR/config"
cat ~/.config/linebot/selected_model 2>/dev/null | xargs basename > "$BACKUP_DIR/config/selected_model_name.txt" && \
    echo "  ✅ 目前模型：$(cat $BACKUP_DIR/config/selected_model_name.txt)"

# ── 6. Systemd 服務定義 ──────────────────────────────────────
echo ""
echo "📦 備份 systemd 服務..."
mkdir -p "$BACKUP_DIR/systemd"
for svc in linebot-llama linebot-flask linebot-admin linebot-tunnel; do
    src="$HOME/.config/systemd/user/${svc}.service"
    if [ -f "$src" ]; then
        cp "$src" "$BACKUP_DIR/systemd/" && echo "  ✅ ${svc}.service"
    fi
done

# ── 7. 產生 .env 範本（key 保留，value 清空）────────────────
echo ""
echo "📦 產生 .env 範本（值已清空，需手動填入）..."
if [ -f "$HOME/文件/line_bot/.env" ]; then
    grep -oP '^[A-Z_]+(?==)' "$HOME/文件/line_bot/.env" | \
        awk '{print $0"=<請填入>"}' > "$BACKUP_DIR/config/env_template.txt" && \
        echo "  ✅ env_template.txt（$(wc -l < $BACKUP_DIR/config/env_template.txt) 個變數）"
fi

# ── 8. 壓縮打包 ──────────────────────────────────────────────
echo ""
echo "📦 壓縮備份..."
ARCHIVE="$HOME/文件/output/antigravity_backup_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf "$ARCHIVE" -C "$(dirname $BACKUP_DIR)" "$(basename $BACKUP_DIR)"
rm -rf "$BACKUP_DIR"

echo ""
echo "=========================================="
echo "✅ 備份完成！"
echo "📁 備份檔：$ARCHIVE"
echo "📊 大小：$(du -sh $ARCHIVE | cut -f1)"
echo ""
echo "新機器安裝步驟："
echo "  1. 安裝 Antigravity"
echo "  2. tar -xzf antigravity_backup_*.tar.gz"
echo "  3. 複製設定到對應位置（見備份內的 workspace/）"
echo "  4. 從舊機器 rsync skills："
echo "     rsync -avz pipadmin@100.115.36.53:~/.gemini/antigravity/skills/ ~/.gemini/antigravity/skills/"
echo "  5. 手動填入 .env（參考 config/env_template.txt）"
echo "  6. bash ~/文件/install_services.sh"
echo "=========================================="
