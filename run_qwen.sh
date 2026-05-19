#!/bin/bash
# 設定解除鎖定記憶體的限制
# ulimit -l unlimited

./llama.cpp/build/bin/llama-cli \
  -m ~/文件/models/Qwen3.6-35B-A3B-Claude-4.6-Opus-Reasoning-Distilled.Q4_K_M.gguf \
  --n-cpu-moe 36 \
  --no-mmap \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  -c 128000 \
  -p "你好，請介紹一下你自己！" \
  -n -1
