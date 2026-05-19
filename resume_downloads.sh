#!/bin/bash
cd /home/pipadmin/文件/models

echo "繼續下載 Qwen3.6-35B-A3B-uncensored-heretic..."
wget -c "https://huggingface.co/llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF/resolve/main/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q4_K_M.gguf" -O "Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-Q4_K_M.gguf"

echo "開始下載 NVIDIA-Nemotron-3-Nano-Omni-30B..."
wget -c "https://huggingface.co/unsloth/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-GGUF/resolve/main/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-Q4_K_M.gguf" -O "NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-Q4_K_M.gguf"

echo "下載完成！"
