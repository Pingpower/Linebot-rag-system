#!/bin/bash

# 定義模型目錄
MODELS_DIR="$HOME/文件/models"

# 檢查目錄是否存在
if [ ! -d "$MODELS_DIR" ]; then
    echo "找不到模型目錄: $MODELS_DIR"
    exit 1
fi

# 取得所有 .gguf 檔案
shopt -s nullglob
MODELS=("$MODELS_DIR"/*.gguf)

if [ ${#MODELS[@]} -eq 0 ]; then
    echo "在 $MODELS_DIR 中找不到任何 .gguf 模型檔案。"
    exit 1
fi

echo -e "\e[36m==================================================\e[0m"
echo -e "\e[1;33m       歡迎使用 AI API 伺服器啟動器 (llama-server)      \e[0m"
echo -e "\e[36m==================================================\e[0m"
echo "請選擇要作為後台大腦的模型："
echo ""

# 列出模型
for i in "${!MODELS[@]}"; do
    filename=$(basename "${MODELS[$i]}")
    echo -e "  \e[1;32m[$((i+1))]\e[0m $filename"
done
echo ""

# 讓使用者選擇
read -p "請輸入數字 (1-${#MODELS[@]}): " choice

# 驗證輸入
if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#MODELS[@]}" ]; then
    echo -e "\e[31m無效的選擇，即將離開。\e[0m"
    exit 1
fi

SELECTED_MODEL="${MODELS[$((choice-1))]}"
echo ""
echo -e "您選擇了: \e[1;36m$(basename "$SELECTED_MODEL")\e[0m"
echo "正在載入系統最佳化參數 (GTX 1060 6GB + 32GB RAM)..."
echo -e "伺服器即將在 \e[1;32mhttp://127.0.0.1:8080\e[0m 啟動..."
echo -e "\e[36m==================================================\e[0m\n"

# 啟動 llama-server
# --port 8080: 預設監聽埠
# --host 0.0.0.0: 允許區域網路連線
$HOME/文件/llama.cpp/build/bin/llama-server \
  -m "$SELECTED_MODEL" \
  --n-cpu-moe 36 \
  --no-mmap \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  -c 128000 \
  --host 0.0.0.0 \
  --port 8080
