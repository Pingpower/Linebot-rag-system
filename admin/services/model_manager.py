import os
import re
import time
import json
import logging
import threading
import subprocess
import requests as req_lib
import uuid
from collections import deque

from config import logger
import config

def get_server_metrics():
    """Wrapper to dynamically access patched server metrics from config."""
    return config.get_server_metrics()

# Global variables for model download status and model switching status
DOWNLOAD_STATUS = {}
SWITCH_STATUS = {
    'status': 'idle',   # 'idle' | 'switching' | 'success' | 'failed'
    'model_name': '',
    'error': None,
    'message': '',
    'last_updated': 0
}

# Global thread locks to prevent race conditions during model switches
model_switch_lock = threading.Lock()

def _write_llama_service_file(m_path, t, g, c, service_path):
    """Helper to write Systemd user service file for llama-server."""
    extra_args = []
    m_path_lower = m_path.lower()
    if any(kw in m_path_lower for kw in ["moe", "a3b", "mixtral", "dbrx", "a1b", "lfm"]):
        extra_args.append("--cpu-moe")
    extra_args.append("--no-mmap")
    extra_args.append("--mlock")
    
    # Enable Flash Attention only if GPU offloading is active (g > 0)
    if g > 0:
        extra_args.append("--flash-attn on")
        
    # Auto-detect mmproj (Multimodal projection)
    m_dir = os.path.dirname(m_path)
    m_name = os.path.basename(m_path)
    mmproj_path = os.path.join(m_dir, f"mmproj-{m_name}")
    if os.path.exists(mmproj_path):
        extra_args.append(f"--mmproj {mmproj_path}")
        
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
    --reasoning off \\
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

def _read_last_log_lines_optimized(log_path, max_lines=20):
    """Read the last N lines of a file efficiently by seeking to the end."""
    if not os.path.exists(log_path):
        return ""
    try:
        file_size = os.path.getsize(log_path)
        # Seek to the end minus 10KB, or 0 if file is smaller than 10KB
        offset = max(0, file_size - 10240)
        with open(log_path, "rb") as f:
            f.seek(offset)
            content_bytes = f.read()
            content = content_bytes.decode('utf-8', errors='ignore')
            
        # Use deque to get last N lines from the string content
        lines = content.splitlines(keepends=True)
        last_lines = list(deque(lines, maxlen=max_lines))
        return "".join(last_lines)
    except Exception as e:
        logger.error(f"Failed to read log optimized: {e}")
        # Fallback to simple deque reading if seek fails
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                return "".join(deque(f, maxlen=max_lines))
        except Exception:
            return ""

def _download_model_worker(model_id, download_url, save_path):
    global DOWNLOAD_STATUS
    local_run_id = str(uuid.uuid4())
    DOWNLOAD_STATUS[model_id] = {
        "status": "downloading",
        "percent": 0,
        "downloaded_mb": 0,
        "total_mb": 0,
        "speed": "0MB/s",
        "error": "",
        "run_id": local_run_id
    }
    
    temp_save_path = save_path + ".tmp"
    max_retries = 15
    retry_delay = 5
    
    for attempt in range(max_retries):
        task = DOWNLOAD_STATUS.get(model_id)
        if not task:
            try:
                if os.path.exists(temp_save_path):
                    os.remove(temp_save_path)
            except:
                pass
            return
        elif task.get("run_id") != local_run_id:
            return
            
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            # 1. Get partially downloaded file size locally
            existing_size = 0
            if os.path.exists(temp_save_path):
                existing_size = os.path.getsize(temp_save_path)
                
            # 2. Get remote file size
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

            # 3. If downloaded size meets or exceeds remote size, complete the process
            if remote_total_size > 0 and existing_size >= remote_total_size:
                task = DOWNLOAD_STATUS.get(model_id)
                if not task:
                    try:
                        if os.path.exists(temp_save_path):
                            os.remove(temp_save_path)
                    except:
                        pass
                    return
                elif task.get("run_id") != local_run_id:
                    return
                    
                if os.path.exists(save_path):
                    try:
                        os.remove(save_path)
                    except:
                        pass
                os.rename(temp_save_path, save_path)
                task = DOWNLOAD_STATUS.get(model_id)
                if task and task.get("run_id") == local_run_id:
                    DOWNLOAD_STATUS[model_id].update({
                        "status": "completed",
                        "percent": 100,
                        "downloaded_mb": round(remote_total_size / (1024 * 1024), 1),
                        "total_mb": round(remote_total_size / (1024 * 1024), 1),
                        "speed": "0MB/s",
                        "error": ""
                    })
                return

            # 4. Prepare HTTP Range request
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
            task = DOWNLOAD_STATUS.get(model_id)
            if task and task.get("run_id") == local_run_id:
                DOWNLOAD_STATUS[model_id]["total_mb"] = total_mb
            else:
                if not task:
                    try:
                        if os.path.exists(temp_save_path):
                            os.remove(temp_save_path)
                    except:
                        pass
                return
            
            start_time = time.time()
            chunk_downloaded_this_session = 0
            
            with open(temp_save_path, write_mode) as f:
                for chunk in response.iter_content(chunk_size=512*1024):  # 512KB chunks
                    task = DOWNLOAD_STATUS.get(model_id)
                    if not task or task.get("run_id") != local_run_id:
                        logger.info(f"Download task {model_id} has been cancelled or overridden. Terminating download thread.")
                        if not task:
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
                    
                    task = DOWNLOAD_STATUS.get(model_id)
                    if task and task.get("run_id") == local_run_id:
                        DOWNLOAD_STATUS[model_id].update({
                            "percent": percent,
                            "downloaded_mb": downloaded_mb,
                            "speed": f"{speed}MB/s",
                            "error": ""
                        })
                    else:
                        if not task:
                            try:
                                if os.path.exists(temp_save_path):
                                    os.remove(temp_save_path)
                            except:
                                pass
                        return
                    
            # 5. Download complete, rename temp file to target filename
            task = DOWNLOAD_STATUS.get(model_id)
            if not task or task.get("run_id") != local_run_id:
                if not task:
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
            
            task = DOWNLOAD_STATUS.get(model_id)
            if task and task.get("run_id") == local_run_id:
                DOWNLOAD_STATUS[model_id]["status"] = "completed"
            else:
                if not task:
                    try:
                        if os.path.exists(save_path):
                            os.remove(save_path)
                    except:
                        pass
                return
            logger.info(f"Model {model_id} downloaded successfully to {save_path}")
            return
            
        except Exception as e:
            task = DOWNLOAD_STATUS.get(model_id)
            if not task or task.get("run_id") != local_run_id:
                if not task:
                    try:
                        if os.path.exists(temp_save_path):
                            os.remove(temp_save_path)
                    except:
                        pass
                return
            logger.error(f"Download attempt {attempt+1}/{max_retries} failed for {model_id}: {e}")
            task = DOWNLOAD_STATUS.get(model_id)
            if task and task.get("run_id") == local_run_id:
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
            else:
                if not task:
                    try:
                        if os.path.exists(temp_save_path):
                            os.remove(temp_save_path)
                    except:
                        pass
                return

def _get_model_size_billion(model_id):
    """Estimate model size in Billion parameters based on model ID string pattern."""
    model_id_lower = model_id.lower()
    
    # 1. Match A\d+B structure (e.g. A3B, A4B, A3.5B)
    active_match = re.search(r'a(\d+(?:\.\d+)?)\s*b', model_id_lower)
    if active_match:
        try:
            return float(active_match.group(1))
        except ValueError:
            pass
            
    # 2. Match MoE structure (e.g. 8x7b), estimating runtime size by active experts (expert_size * 2)
    moe_match = re.search(r'(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b', model_id_lower)
    if moe_match:
        try:
            expert_size = float(moe_match.group(2))
            return expert_size * 2.0
        except ValueError:
            pass

    # 3. Match regular 7b, 8b, 14b, 1.5b etc.
    matches = re.findall(r'(\d+(?:\.\d+)?)\s*b', model_id_lower)
    if matches:
        try:
            return float(matches[-1])
        except ValueError:
            pass
            
    # 4. Match 500m tiny models
    matches_m = re.findall(r'(\d+(?:\.\d+)?)\s*m', model_id_lower)
    if matches_m:
        try:
            return float(matches_m[-1]) / 1000.0
        except ValueError:
            pass
            
    return 7.0

def _is_model_suitable(model_size_b, vram_gb):
    """Assess if the model is runnable on the current system VRAM."""
    if vram_gb <= 0:  # CPU-only mode
        return model_size_b <= 8.0
    elif vram_gb <= 6.5:
        return model_size_b <= 9.5
    elif vram_gb <= 12.5:
        return model_size_b <= 15.5
    elif vram_gb <= 16.5:
        return model_size_b <= 22.5
    else:
        return model_size_b <= 34.5

def _is_file_suitable(file_size_bytes, vram_gb, filename=""):
    """Check if model file size is compatible with hardware limitations."""
    file_size_gb = file_size_bytes / (1024 * 1024 * 1024)
    # Ignore empty or corrupted tiny dummy files (<10MB)
    if file_size_gb < 0.01:
        return False
        
    filename_lower = filename.lower()
    is_moe = "moe" in filename_lower or "8x" in filename_lower or re.search(r'a\d+b', filename_lower) is not None
    
    if is_moe:
        # Since local RAM is 32GB, allow executing up to 26GB MoE model with CPU hybrid offloading
        return file_size_gb <= 26.0
        
    if vram_gb <= 0:  # CPU-only mode
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
    Uses safe subprocess calls to check current GPU compute apps.
    Falls back to a flat 3.0 second sleep if nvidia-smi is missing.
    """
    start_time = time.time()
    logger.info("Starting wait_for_llama_vram_clear to avoid race conditions...")
    nvidia_smi_available = False
    
    try:
        res = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
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
            ["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
            capture_output=True, text=True
        )
        if "llama-server" not in res.stdout:
            logger.info("llama-server is not in GPU compute apps. VRAM is considered released.")
            return True
        time.sleep(0.5)

    logger.warning("VRAM clear wait timed out, proceeding anyway to start engine.")
    return False

def _switch_model_worker(selected_path, model_name, old_selected_path, old_cfg, threads, gpu_layers, ctx_size, selected_model_file, service_path, config_path):
    global SWITCH_STATUS
    try:
        SWITCH_STATUS['status'] = 'switching'
        SWITCH_STATUS['model_name'] = model_name
        SWITCH_STATUS['error'] = None
        SWITCH_STATUS['message'] = f'正在套用參數並重啟服務，準備載入模型 {model_name}...'
        SWITCH_STATUS['last_updated'] = time.time()
        
        # 1. Determine local system hardware capability if parameter estimation is needed
        cpu_count = os.cpu_count() or 8
        default_threads = max(4, cpu_count // 2)
        
        # Directly read total VRAM size (MB) cached in system metrics, avoiding slow subprocess calls
        metrics = get_server_metrics()
        vram_total_mb = metrics.get('vram_total', 0)
        
        # 2. Apply default fallback or dynamically estimate parameters if None (no user custom config)
        if threads is None:
            threads = default_threads
        if ctx_size is None:
            ctx_size = 4096
        if gpu_layers is None:
            if vram_total_mb == 0:  # CPU mode
                gpu_layers = 0
            else:
                vram_total_gb = vram_total_mb / 1024.0
                file_size_gb = 0.0
                if os.path.exists(selected_path):
                    file_size_gb = os.path.getsize(selected_path) / (1024 * 1024 * 1024)
                
                model_size_b = _get_model_size_billion(model_name)
                if model_size_b == 7.0:
                    model_size_b = round(file_size_gb * 1.6, 1)
                
                # Keep 1.5 GB VRAM headroom for the host OS to prevent CUDA OOM
                available_vram_gb = max(vram_total_gb - 1.5, 0.0)
                
                if available_vram_gb <= 0:
                    gpu_layers = 0
                else:
                    # Estimate total model layers based on Billion scale
                    if model_size_b <= 4.0:
                        total_layers = 28
                    elif model_size_b <= 10.0:
                        total_layers = 32
                    elif model_size_b <= 16.0:
                        total_layers = 40
                    else:
                        total_layers = 60
                    
                    weight_per_layer = file_size_gb / total_layers
                    estimated_vram_per_layer = weight_per_layer + 0.015  # weight + extra KV cache/compute overhead
                    
                    g = int(available_vram_gb / estimated_vram_per_layer)
                    if g >= total_layers:
                        gpu_layers = 99  # offload all layers to GPU
                    else:
                        gpu_layers = max(g, 0)
        
        logger.info(f"Target model launch settings: threads={threads}, gpu_layers={gpu_layers}, ctx_size={ctx_size}")

        # Save current settings to persist user parameter tuning
        try:
            with open(config_path, 'w') as cf:
                json.dump({'threads': threads, 'gpu_layers': gpu_layers, 'ctx_size': ctx_size}, cf)
        except Exception as e:
            logger.error(f"Failed to auto-save model config: {e}")

        with open(selected_model_file, "w") as f:
            f.write(selected_path)

        _write_llama_service_file(selected_path, threads, gpu_layers, ctx_size, service_path)
            
        # 3. Reload and restart systemd service (Safe subprocess calls without shell=True)
        SWITCH_STATUS['message'] = '正在重新載入 Systemd 並重啟 linebot-llama 服務...'
        SWITCH_STATUS['last_updated'] = time.time()
        
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        # Kill the llama processes accurately first to prevent systemctl stop from blocking
        subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", "linebot-llama"], check=False)
        subprocess.run(["systemctl", "--user", "stop", "linebot-llama"], check=False)
        
        wait_for_llama_vram_clear()
        
        subprocess.run(["systemctl", "--user", "start", "linebot-llama"], check=True)
        
        # 4. Monitor service initialization health
        SWITCH_STATUS['message'] = '服務已啟動，正在載入模型檔案至記憶體/顯存 (最長等待 40 秒)...'
        SWITCH_STATUS['last_updated'] = time.time()
        is_active = False
        for i in range(40):
            status_check = subprocess.run(["systemctl", "--user", "is-active", "linebot-llama"], capture_output=True, text=True)
            if status_check.stdout.strip() != "active":
                break
            try:
                h_resp = req_lib.get("http://127.0.0.1:8080/health", timeout=1.0)
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
                    # Read last 20 lines of logs using optimized seek
                    log_text = _read_last_log_lines_optimized(log_path, max_lines=20)
                    
                    if "data is not within the file bounds" in log_text or "corrupted or incomplete" in log_text or "failed to read" in log_text:
                        err_msg = "模型載入失敗：該模型檔案已損壞，可能是下載中斷或不完整，建議刪除重新下載！"
                    elif "cudaError" in log_text or "CUDA error" in log_text or "out of memory" in log_text or "CUDA_ERROR_OUT_OF_MEMORY" in log_text:
                        err_msg = "顯卡記憶體 (VRAM) 不足！無法承載此微調參數，請調低 GPU 卸載層數 (建議設為 0) 或縮小上下文大小。"
                    elif "failed to load model" in log_text:
                        err_msg = "引擎載入模型失敗，請確認檔案格式是否正確且完整。"
                    else:
                        err_msg = f"引擎啟動失敗，日誌最後 20 行：\n{log_text}"
                except Exception as le:
                    logger.error(f"Read llama.log error: {le}")
            
            # Rollback procedure if startup fails
            SWITCH_STATUS['message'] = f'錯誤：{err_msg} 正在執行自動回滾...'
            SWITCH_STATUS['last_updated'] = time.time()
            
            if old_selected_path and os.path.exists(old_selected_path) and old_selected_path != selected_path:
                with open(selected_model_file, "w") as f:
                    f.write(old_selected_path)
                _write_llama_service_file(old_selected_path, old_cfg.get('threads', 8), old_cfg.get('gpu_layers', 10), old_cfg.get('ctx_size', 8192), service_path)
                
                subprocess.run(["systemctl", "--user", "daemon-reload"])
                subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", "linebot-llama"], check=False)
                subprocess.run(["systemctl", "--user", "stop", "linebot-llama"], check=False)
                
                wait_for_llama_vram_clear()
                subprocess.run(["systemctl", "--user", "start", "linebot-llama"])
                
                SWITCH_STATUS.update({
                    'status': 'failed',
                    'error': err_msg,
                    'message': f'切換失敗：{err_msg} 已自動回滾至先前工作的模型。',
                    'last_updated': time.time()
                })
            else:
                safe_threads = default_threads
                safe_gpu = 0
                safe_ctx = 2048
                _write_llama_service_file(selected_path, safe_threads, safe_gpu, safe_ctx, service_path)
                
                subprocess.run(["systemctl", "--user", "daemon-reload"])
                subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", "linebot-llama"], check=False)
                subprocess.run(["systemctl", "--user", "stop", "linebot-llama"], check=False)
                
                wait_for_llama_vram_clear()
                subprocess.run(["systemctl", "--user", "start", "linebot-llama"])
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
    finally:
        try:
            model_switch_lock.release()
        except RuntimeError:
            pass

def _config_apply_worker(config_data, old_config, config_path, selected_path, service_path, selected_model_path):
    global SWITCH_STATUS
    try:
        SWITCH_STATUS['status'] = 'switching'
        SWITCH_STATUS['model_name'] = os.path.basename(selected_path)
        SWITCH_STATUS['error'] = None
        SWITCH_STATUS['message'] = '正在套用微調配置並重啟服務，請稍候...'
        SWITCH_STATUS['last_updated'] = time.time()

        threads = config_data['threads']
        gpu_layers = config_data['gpu_layers']
        ctx_size = config_data['ctx_size']

        _write_llama_service_file(selected_path, threads, gpu_layers, ctx_size, service_path)

        SWITCH_STATUS['message'] = '正在重新載入 Systemd 並重啟 linebot-llama 服務...'
        SWITCH_STATUS['last_updated'] = time.time()
        
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", "linebot-llama"], check=False)
        subprocess.run(["systemctl", "--user", "stop", "linebot-llama"], check=False)
        wait_for_llama_vram_clear()
        subprocess.run(["systemctl", "--user", "start", "linebot-llama"], check=True)

        SWITCH_STATUS['message'] = '已重啟服務，正在檢查引擎狀態...'
        SWITCH_STATUS['last_updated'] = time.time()
        time.sleep(2.5)

        status_check = subprocess.run(["systemctl", "--user", "is-active", "linebot-llama"], capture_output=True, text=True)
        is_active = status_check.stdout.strip() == "active"

        if not is_active:
            err_msg = "新微調參數導致引擎啟動失敗。"
            log_path = "/home/pipadmin/文件/llama.log"
            if os.path.exists(log_path):
                try:
                    log_text = _read_last_log_lines_optimized(log_path, max_lines=20)
                    if "cudaError" in log_text or "CUDA error" in log_text or "out of memory" in log_text or "CUDA_ERROR_OUT_OF_MEMORY" in log_text:
                        err_msg = "新微調參數導致顯卡記憶體 (VRAM) 不足！請調低 GPU 卸載層數或縮小上下文大小。"
                    else:
                        err_msg = f"新微調參數導致引擎啟動失敗。日誌最後 20 行：\n{log_text}"
                except Exception:
                    pass
            
            # Rollback config
            with open(config_path, 'w') as f:
                json.dump(old_config, f)
            _write_llama_service_file(selected_path, old_config.get('threads', 8), old_config.get('gpu_layers', 10), old_config.get('ctx_size', 8192), service_path)
            
            subprocess.run(["systemctl", "--user", "daemon-reload"])
            subprocess.run(["systemctl", "--user", "kill", "-s", "SIGKILL", "linebot-llama"], check=False)
            subprocess.run(["systemctl", "--user", "stop", "linebot-llama"], check=False)
            wait_for_llama_vram_clear()
            subprocess.run(["systemctl", "--user", "start", "linebot-llama"])

            SWITCH_STATUS.update({
                'status': 'failed',
                'error': err_msg,
                'message': f'錯誤：{err_msg} 已自動回滾至先前的微調配置。',
                'last_updated': time.time()
            })
        else:
            SWITCH_STATUS.update({
                'status': 'success',
                'message': '微調配置已成功儲存並套用！',
                'last_updated': time.time()
            })
    except Exception as e:
        logger.error(f"Error in background config apply worker: {e}")
        SWITCH_STATUS.update({
            'status': 'failed',
            'error': str(e),
            'message': f'套用微調配置失敗，發生未預期錯誤：{str(e)}',
            'last_updated': time.time()
        })
    finally:
        try:
            model_switch_lock.release()
        except RuntimeError:
            pass