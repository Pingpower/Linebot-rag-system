#!/bin/bash
echo "Stopping existing llama-server..."
pkill llama-server
sleep 2

echo "Starting MTP Llama-server..."
nohup /home/pipadmin/文件/llama.cpp/build/bin/llama-server \
  --model /home/pipadmin/文件/models/gemma-4-12b-it-qat-q4_0.gguf \
  --model-draft /home/pipadmin/文件/models/gemma-4-12B-it-assistant-MTP-F16.gguf \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 4096 \
  --n-gpu-layers 99 \
  --threads 6 \
  --threads-batch 6 \
  --parallel 1 \
  --no-mmap \
  --mlock \
  --flash-attn on \
  --log-disable > /home/pipadmin/文件/llama_mtp.log 2>&1 &

echo "MTP llama-server is now running in the background."
echo "Log file: /home/pipadmin/文件/llama_mtp.log"
