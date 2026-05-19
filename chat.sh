#!/bin/bash

# 設定模型資料夾路徑
MODELS_DIR="$HOME/文件/models"

# 檢查資料夾是否存在
if [ ! -d "$MODELS_DIR" ]; then
    echo -e "\e[31m錯誤：找不到模型資料夾 $MODELS_DIR\e[0m"
    exit 1
fi

echo -e "\e[36m==================================================\e[0m"
echo -e "\e[1;36m       🤖 Antigravity AI 動態模型啟動器        \e[0m"
echo -e "\e[36m==================================================\e[0m"
echo -e "正在掃描模型檔案...\n"

# 找出所有的 .gguf 檔案並存入陣列
# 使用 find 避免路徑中有空白的問題
mapfile -t MODEL_FILES < <(find "$MODELS_DIR" -maxdepth 1 -name "*.gguf" -type f | sort)

if [ ${#MODEL_FILES[@]} -eq 0 ]; then
    echo -e "\e[31m在 $MODELS_DIR 中找不到任何 .gguf 模型檔案！\e[0m"
    echo "請先下載模型後再重試。"
    exit 1
fi

# 印出選項清單
for i in "${!MODEL_FILES[@]}"; do
    FILENAME=$(basename "${MODEL_FILES[$i]}")
    echo -e "  \e[1;33m[$((i+1))]\e[0m \e[32m$FILENAME\e[0m"
done

echo ""
echo -e "  \e[1;33m[0]\e[0m  離開程式"
echo ""

# 讀取使用者輸入
while true; do
    read -p "👉 請輸入您想啟動的模型編號 [1-${#MODEL_FILES[@]}]: " choice
    if [[ "$choice" == "0" ]]; then
        echo "已取消啟動。再見！"
        exit 0
    elif [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#MODEL_FILES[@]}" ]; then
        SELECTED_MODEL="${MODEL_FILES[$((choice-1))]}"
        break
    else
        echo -e "\e[31m❌ 無效的輸入，請重新輸入數字！\e[0m"
    fi
done

SELECTED_FILENAME=$(basename "$SELECTED_MODEL")

echo -e "\n\e[36m==================================================\e[0m"
echo -e "🚀 準備啟動：\e[1;32m$SELECTED_FILENAME\e[0m"
echo -e "⚙️  套用 1060 6GB 最佳化參數 (128K 上下文, Q4快取)..."
echo -e "\e[36m==================================================\e[0m\n"

# 啟動 llama.cpp
# -cnv: 啟用互動式對話模式 (Chat mode)
# -sys: 設定系統提示詞，教導模型如何判斷何時需要深度思考

$HOME/文件/llama.cpp/build/bin/llama-cli \
  -m "$SELECTED_MODEL" \
  --n-cpu-moe 36 \
  --no-mmap \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  -c 128000 \
  -cnv \
  -sys "你是一個聰明、高效且友善的專屬 AI 秘書。請直接、快速、俐落地回答使用者的問題，不拖泥帶水。" \
  -n -1
