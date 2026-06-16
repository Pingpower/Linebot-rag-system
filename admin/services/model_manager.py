import os
import re
import time
import json
import logging
import threading
import subprocess
import requests as req_lib

from config import logger, get_server_metrics

# 全域模型下載進度追蹤器與切換狀態
DOWNLOAD_STATUS = {}
SWITCH_STATUS = {
    'status': 'idle',   # 'idle' | 'switching' | 'success' | 'failed'
    'model_name': '',
    'error': None,
    'message': '',
    'last_updated': 0
}

def _download_model_worker(model_id, download_url, save_path):
    global DOWNLOAD_STATUS
    DOWNLOAD_STATUS[model_id] = {
        "status": "downloading",
        "percent": 0,
        "downloaded_mb": 0,
        "total_mb": 0,
        "speed": "0MB/s",
        "error": ""
    }
    
    temp_save_path = save_path + ".tmp"
    max_retries = 15
    retry_delay = 5
    
    for attempt in range(max_retries):
        if model_id not in DOWNLOAD_STATUS:
            return
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            # 1. 取得本機已下載的部分大小
            existing_size = 0
            if os.path.exists(temp_save_path):
                existing_size = os.path.getsize(temp_save_path)
                
            # 2. 獲取遠端檔案總大小
            remote_total_size = 0
            try:
                head_res = req_lib.head(download_url, timeout=20, allow_redirects=True)
                if head_res.status_code in [200, 206]:
                    content_length = head_res.headers.get('content-length')
                    remote_total_size = int(content_length) if content_length else 0
                    cr = head_res.headers.get('Content-Range')
                    if cr and '/' in cr:
                        try:
                            remote_total_size = int(cr.split('/')[-1])
                        except:
                            pass
            except Exception as e:
                logger.warning(f"HEAD request failed, fallback to GET range: {e}")
                
            if remote_total_size == 0:
                try:
                    test_res = req_lib.get(download_url, headers={'Range': 'bytes=0-0'}, timeout=20)
                    cr = test_res.headers.get('Content-Range')
                    if cr and '/' in cr:
                        remote_total_size = int(cr.split('/')[-1])
                except Exception as e:
                    logger.error(f"Failed to get remote size via fallback GET: {e}")

            # 3. 若已下載大小大於等於遠端檔案大小，直接重新命名並完成
            if remote_total_size > 0 and existing_size >= remote_total_size:
                if model_id not in DOWNLOAD_STATUS:
                    try:
                        if os.path.exists(temp_save_path):
                            os.remove(temp_save_path)
                    except:
                        pass
                    return
                if os.path.exists(save_path):
                    try:
                        os.remove(save_path)
                    except:
                        pass
                os.rename(temp_save_path, save_path)
                if model_id in DOWNLOAD_STATUS:
                    DOWNLOAD_STATUS[model_id].update({
                        "status": "completed",
                        "percent": 100,
                        "downloaded_mb": round(remote_total_size / (1024 * 1024), 1),
                        "total_mb": round(remote_total_size / (1024 * 1024), 1),
                        "speed": "0MB/s",
                        "error": ""
                    })
                return

            # 4. 準備 HTTP Range 請求
            headers = {}
            if existing_size > 0:
                headers['Range'] = f"bytes={existing_size}-"
                
            response = req_lib.get(download_url, headers=headers, stream=True, timeout=45)
            
            if response.status_code == 206:
                content_length = response.headers.get('content-length')
                total_size = existing_size + (int(content_length) if content_length else 0)
                write_mode = 'ab'
                downloaded = existing_size
            else:
                response.raise_for_status()
                content_length = response.headers.get('content-length')
                total_size = int(content_length) if content_length else 0
                write_mode = 'wb'
                downloaded = 0
                
            total_mb = round(total_size / (1024 * 1024), 1)
            if model_id in DOWNLOAD_STATUS:
                DOWNLOAD_STATUS[model_id]["total_mb"] = total_mb
            
            start_time = time.time()
            chunk_downloaded_this_session = 0
            
            with open(temp_save_path, write_mode) as f:
                for chunk in response.iter_content(chunk_size=512*1024):  # 512KB chunks
                    if model_id not in DOWNLOAD_STATUS:
                        logger.info(f"Download task {model_id} has been cancelled by user. Terminating download thread.")
                        try:
                            if os.path.exists(temp_save_path):
                                os.remove(temp_save_path)
                        except Exception as e:
                            logger.error(f"Failed to remove temp file {temp_save_path}: {e}")
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    chunk_downloaded_this_session += len(chunk)
                    downloaded_mb = round(downloaded / (1024 * 1024), 1)
                    
                    elapsed = time.time() - start_time
                    session_downloaded_mb = chunk_downloaded_this_session / (1024 * 1024)
                    speed = round(session_downloaded_mb / elapsed, 2) if elapsed > 0 else 0
                    percent = int(100 * downloaded / total_size) if total_size > 0 else 0
                    
                    if model_id in DOWNLOAD_STATUS:
                        DOWNLOAD_STATUS[model_id].update({
                            "percent": percent,
                            "downloaded_mb": downloaded_mb,
                            "speed": f"{speed}MB/s",
                            "error": ""
                        })
                    
            # 5. 下載完成，重命名為正式檔名
            if model_id not in DOWNLOAD_STATUS:
                try:
                    if os.path.exists(temp_save_path):
                        os.remove(temp_save_path)
                except Exception as e:
                    logger.error(f"Failed to remove temp file {temp_save_path} on completion-cancellation: {e}")
                return

            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except:
                    pass
            os.rename(temp_save_path, save_path)
            
            if model_id in DOWNLOAD_STATUS:
                DOWNLOAD_STATUS[model_id]["status"] = "completed"
            logger.info(f"Model {model_id} downloaded successfully to {save_path}")
            return
            
        except Exception as e:
            if model_id not in DOWNLOAD_STATUS:
                try:
                    if os.path.exists(temp_save_path):
                        os.remove(temp_save_path)
                except:
                    pass
                return
            logger.error(f"Download attempt {attempt+1}/{max_retries} failed for {model_id}: {e}")
            if attempt == max_retries - 1:
                DOWNLOAD_STATUS[model_id].update({
                    "status": "failed",
                    "error": f"下載失敗 (已達最大重試次數): {str(e)}"
                })
            else:
                DOWNLOAD_STATUS[model_id].update({
                    "status": "downloading",
                    "error": f"連線異常，正在進行第 {attempt+1} 次自動重試... ({str(e)})",
                    "speed": "自動重連中"
                })
                time.sleep(retry_delay)


def _get_model_size_billion(model_id):
    model_id_lower = model_id.lower()
    
    # 1. 優先匹配 A\d+B 格式，例如 A3B, A4B, A3.5B
    active_match = re.search(r'a(\d+(?:\.\d+)?)\s*b', model_id_lower)
    if active_match:
        try:
            return float(active_match.group(1))
        except ValueError:
            pass
            
    # 2. 匹配 MoE 8x7b 等格式，以每次啟動 2 個專家 (expert_size * 2) 估算運作大小
    moe_match = re.search(r'(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b', model_id_lower)
    if moe_match:
        try:
            expert_size = float(moe_match.group(2))
            return expert_size * 2.0
        except ValueError:
            pass

    # 3. 匹配常規 7b, 8b, 14b, 1.5b 等
    matches = re.findall(r'(\d+(?:\.\d+)?)\s*b', model_id_lower)
    if matches:
        try:
            return float(matches[-1])
        except ValueError:
            pass
            
    # 4. 匹配 500m 等極小模型
    matches_m = re.findall(r'(\d+(?:\.\d+)?)\s*m', model_id_lower)
    if matches_m:
        try:
            return float(matches_m[-1]) / 1000.0
        except ValueError:
            pass
            
    return 7.0


def _is_model_suitable(model_size_b, vram_gb):
    if vram_gb <= 0:  # 純 CPU 模式
        return model_size_b <= 8.0  # CPU 最多跑 8B，再大會極度卡頓
    elif vram_gb <= 6.5:
        return model_size_b <= 9.5  # 6GB 舒適跑 9.5B 以下 (如 7B, 8B, 9B)
    elif vram_gb <= 12.5:
        return model_size_b <= 15.5  # 12GB 舒適跑 14B/15B 以下
    elif vram_gb <= 16.5:
        return model_size_b <= 22.5  # 16GB 舒適跑 20B/22B 以下
    else:
        return model_size_b <= 34.5  # 24GB 舒適跑 32B/34B 以下


def _is_file_suitable(file_size_bytes, vram_gb, filename=""):
    file_size_gb = file_size_bytes / (1024 * 1024 * 1024)
    # 過濾小於 10MB 的 dummy 檔案
    if file_size_gb < 0.01:
        return False
        
    # 檢查是否為 MoE 格式，如果是，因為本機有 32GB RAM 可以跑 CPU 混合推理，放寬限制至 26.0 GB
    filename_lower = filename.lower()
    is_moe = "moe" in filename_lower or "8x" in filename_lower or re.search(r'a\d+b', filename_lower) is not None
    
    if is_moe:
        return file_size_gb <= 26.0 # 允許下載並執行 26GB 以下 the MoE 模型
        
    if vram_gb <= 0:  # 純 CPU
        return file_size_gb <= 6.5
    elif vram_gb <= 6.5:
        return file_size_gb <= 8.0
    elif vram_gb <= 12.5:
        return file_size_gb <= 14.0
    elif vram_gb <= 16.5:
        return file_size_gb <= 19.0
    else:
        return file_size_gb <= 32.0


def wait_for_llama_vram_clear(timeout=8):
    """
    Wait for the old llama-server to completely release VRAM from the GPU.
    If nvidia-smi is available, it queries for running CUDA compute apps.
    Falls back to a flat 3.0 second sleep if nvidia-smi is missing.
    """
    start_time = time.time()
    logger.info("Starting wait_for_llama_vram_clear to avoid race conditions...")
    nvidia_smi_available = False
    
    try:
        res = subprocess.run("nvidia-smi -L", shell=True, capture_output=True, text=True)
        if res.returncode == 0:
            nvidia_smi_available = True
    except Exception:
        pass

    if not nvidia_smi_available:
        logger.info("nvidia-smi not available, sleeping for 3.0 seconds as fallback.")
        time.sleep(3.0)
        return True

    while time.time() - start_time < timeout:
        # Check if llama-server is still in GPU compute apps list
        res = subprocess.run(
            "nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader",
            shell=True, capture_output=True, text=True
        )
        if "llama-server" not in res.stdout:
            # Query used memory to ensure the OS and driver finished reclaiming VRAM
            mem_res = subprocess.run(
                "nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits",
                shell=True, capture_output=True, text=True
            )
            try:
                used_mem = int(mem_res.stdout.strip())
                if used_mem < 2500: # Safe threshold (base GPU memory used by X/Gnome is ~300-600MB)
                     logger.info(f"VRAM successfully released. Current GPU memory usage: {used_mem} MiB.")
                     return True
            except Exception:
                logger.info("llama-server cleared from GPU compute apps. Proceeding.")
                return True
        time.sleep(0.5)

    logger.warning("VRAM clear wait timed out, proceeding anyway to start engine.")
    return False


def _switch_model_worker(selected_path, model_name, old_selected_path, old_cfg, threads, gpu_layers, ctx_size, selected_model_file, service_path, config_path):
    global SWITCH_STATUS
    SWITCH_STATUS['status'] = 'switching'
    SWITCH_STATUS['model_name'] = model_name
    SWITCH_STATUS['error'] = None
    SWITCH_STATUS['message'] = f'正在套用參數並重啟服務，準備載入模型 {model_name}...'
    SWITCH_STATUS['last_updated'] = time.time()
    
    def write_service_file(m_path, t, g, c):
        extra_args = []
        m_path_lower = m_path.lower()
        if any(kw in m_path_lower for kw in ["moe", "a3b", "mixtral", "dbrx", "a1b", "lfm"]):
            extra_args.append("--cpu-moe")
        extra_args.append("--no-mmap")
        extra_args.append("--mlock")
        extra_args.append("--flash-attn on")
        extra_str = " ".join(extra_args)
        
        content = f"""[Unit]
Description=LINE Bot LLaMA Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/pipadmin/文件
ExecStart=/home/pipadmin/文件/llama.cpp/build/bin/llama-server \\
    --model {m_path} \\
    --host 127.0.0.1 \\
    --port 8080 \\
    --ctx-size {c} \\
    --n-gpu-layers {g} \\
    --threads {t} \\
    --threads-batch {t} \\
    --parallel 1 \\
    {extra_str} \\
    --log-disable
Restart=always
RestartSec=10
StandardOutput=append:/home/pipadmin/文件/llama.log
StandardError=append:/home/pipadmin/文件/llama.log
Environment=HOME=/home/pipadmin
LimitMEMLOCK=infinity

[Install]
WantedBy=default.target
"""
        with open(service_path, "w") as sf:
            sf.write(content)

    try:
        try:
            with open(config_path, 'w') as cf:
                json.dump({'threads': threads, 'gpu_layers': gpu_layers, 'ctx_size': ctx_size}, cf)
        except Exception as e:
            logger.error(f"Failed to auto-save model config: {e}")

        with open(selected_model_file, "w") as f:
            f.write(selected_path)

        write_service_file(selected_path, threads, gpu_layers, ctx_size)
            
        # 3. Reload and restart service
        SWITCH_STATUS['message'] = '正在重新載入 Systemd 並重啟 linebot-llama 服務...'
        SWITCH_STATUS['last_updated'] = time.time()
        subprocess.run("systemctl --user daemon-reload", shell=True, check=True)
        subprocess.run("systemctl --user stop linebot-llama", shell=True)
        subprocess.run("pkill -9 -f 'llama-server'", shell=True)
        wait_for_llama_vram_clear()
        subprocess.run("systemctl --user start linebot-llama", shell=True, check=True)
        
        # 4. Check status
        SWITCH_STATUS['message'] = '服務已啟動，正在載入模型檔案至記憶體/顯存 (最長等待 40 秒)...'
        SWITCH_STATUS['last_updated'] = time.time()
        is_active = False
        import requests
        for i in range(40):
            status_check = subprocess.run("systemctl --user is-active linebot-llama", shell=True, capture_output=True, text=True)
            if status_check.stdout.strip() != "active":
                break
            try:
                h_resp = requests.get("http://127.0.0.1:8080/health", timeout=1.0)
                if h_resp.status_code == 200:
                    h_data = h_resp.json()
                    if h_data.get('status') == 'ok':
                        is_active = True
                        break
            except Exception:
                pass
            time.sleep(1.0)
        
        if not is_active:
            err_msg = "模型啟動失敗，引擎進程已退出。"
            log_path = "/home/pipadmin/文件/llama.log"
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
                        log_lines = lf.readlines()[-150:]
                    log_text = "".join(log_lines)
                    if "data is not within the file bounds" in log_text or "corrupted or incomplete" in log_text:
                        err_msg = "模型載入失敗：該模型檔案已損壞，可能是下載中斷或不完整，建議刪除重新下載！"
                    elif "cudaError" in log_text or "CUDA error" in log_text or "out of memory" in log_text or "CUDA_ERROR_OUT_OF_MEMORY" in log_text:
                        err_msg = "顯卡記憶體 (VRAM) 不足！GTX 1060 (6GB) 無法承載此微調參數，請調低 GPU 卸載層數 (建議設為 0) 或縮小上下文大小。"
                    elif "failed to load model" in log_text:
                        err_msg = "引擎載入模型失敗，請確認檔案格式是否正確且完整。"
                except Exception as le:
                    logger.error(f"Read llama.log error: {le}")
            
            # Rollback
            SWITCH_STATUS['message'] = f'錯誤：{err_msg} 正在執行自動回滾...'
            SWITCH_STATUS['last_updated'] = time.time()
            if old_selected_path and os.path.exists(old_selected_path) and old_selected_path != selected_path:
                with open(selected_model_file, "w") as f:
                    f.write(old_selected_path)
                write_service_file(old_selected_path, old_cfg.get('threads', 8), old_cfg.get('gpu_layers', 10), old_cfg.get('ctx_size', 8192))
                subprocess.run("systemctl --user daemon-reload", shell=True)
                subprocess.run("systemctl --user stop linebot-llama", shell=True)
                subprocess.run("pkill -9 -f 'llama-server'", shell=True)
                wait_for_llama_vram_clear()
                subprocess.run("systemctl --user start linebot-llama", shell=True)
                SWITCH_STATUS.update({
                    'status': 'failed',
                    'error': err_msg,
                    'message': f'切換失敗：{err_msg} 已自動回滾至先前工作的模型。',
                    'last_updated': time.time()
                })
            else:
                safe_threads = 8
                safe_gpu = 0
                safe_ctx = 2048
                write_service_file(selected_path, safe_threads, safe_gpu, safe_ctx)
                subprocess.run("systemctl --user daemon-reload", shell=True)
                subprocess.run("systemctl --user stop linebot-llama", shell=True)
                subprocess.run("pkill -9 -f 'llama-server'", shell=True)
                wait_for_llama_vram_clear()
                subprocess.run("systemctl --user start linebot-llama", shell=True)
                try:
                    with open(config_path, 'w') as cf:
                        json.dump({'threads': safe_threads, 'gpu_layers': safe_gpu, 'ctx_size': safe_ctx}, cf)
                except Exception:
                    pass
                SWITCH_STATUS.update({
                    'status': 'failed',
                    'error': err_msg,
                    'message': f'切換失敗：{err_msg} 已自動調整為 CPU 預設安全配置（GPU層數=0, Context=2048）重新啟動。',
                    'last_updated': time.time()
                })
        else:
            SWITCH_STATUS.update({
                'status': 'success',
                'message': f'已成功切換至模型 {model_name}。',
                'last_updated': time.time()
            })
    except Exception as e:
        logger.error(f"Error in background switch worker: {e}")
        SWITCH_STATUS.update({
            'status': 'failed',
            'error': str(e),
            'message': f'切換失敗，發生未預期錯誤：{str(e)}',
            'last_updated': time.time()
        })
