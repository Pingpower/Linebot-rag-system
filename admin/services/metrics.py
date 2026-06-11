import os
import subprocess
import time
import requests as req_lib
import threading
import atexit
from config import SYSTEM_METRICS_EVENT, SYSTEM_METRICS_CACHE, logger

def _update_system_metrics_worker():
    """Background worker to periodically update system metrics cache without blocking Flask."""
    logger.info("Background system metrics update thread started.")
    last_idle, last_total = 0.0, 0.0
    try:
        with open('/proc/stat', 'r') as f:
            fields = [float(x) for x in f.readline().strip().split()[1:]]
            last_idle, last_total = fields[3], sum(fields)
    except Exception:
        pass

    while SYSTEM_METRICS_EVENT.is_set():
        try:
            # 1. Detect LLaMA engine status
            try:
                cmd_status = "systemctl --user is-active linebot-llama"
                sys_status = subprocess.check_output(cmd_status, shell=True, text=True).strip()
            except subprocess.CalledProcessError as e:
                sys_status = e.output.strip() if e.output else "inactive"

            llama_url = os.getenv('LLAMA_SERVER_URL', 'http://127.0.0.1:8080')
            status = '離線 (已停止)'
            status_color = '#ef4444'

            if sys_status == "activating":
                status = '啟動中 (加載中)'
                status_color = '#f59e0b'
            elif sys_status == "active":
                try:
                    resp = req_lib.get(f"{llama_url}/health", timeout=2)
                    if resp.status_code == 200:
                        status = '在線 (正常運作)'
                        status_color = '#22c55e'
                    elif resp.status_code == 503 or "Loading model" in resp.text:
                        status = '載入模型中...'
                        status_color = '#f59e0b'
                    else:
                        status = f'異常 (HTTP {resp.status_code})'
                        status_color = '#ef4444'
                except req_lib.exceptions.ConnectionError:
                    status = '啟動中 (載入引擎)...'
                    status_color = '#f59e0b'
                except Exception:
                    status = '異常'
                    status_color = '#ef4444'
            else:
                status = '離線 (已停止)'
                status_color = '#ef4444'

            # 2. Get active model name
            model_name = '無 / 未啟動'
            try:
                cmd_model = "ps -ef | grep '[l]lama-server' | grep -oP '(?<=--model ).*?(?=\\s|$)' || echo ''"
                out_model = subprocess.check_output(cmd_model, shell=True, text=True).strip()
                if out_model:
                    model_name = out_model.split('/')[-1]
            except Exception:
                pass

            # 3. Get CPU usage
            cpu_percent = 0.0
            try:
                with open('/proc/stat', 'r') as f:
                    fields = [float(x) for x in f.readline().strip().split()[1:]]
                idle, total = fields[3], sum(fields)
                idle_delta = idle - last_idle
                total_delta = total - last_total
                if total_delta > 0:
                    cpu_percent = round((1 - idle_delta / total_delta) * 100, 1)
                last_idle, last_total = idle, total
            except Exception:
                pass

            # 4. Get System RAM
            ram_str = '未知'
            ram_used_gb = 0.0
            ram_total_gb = 0.0
            ram_percent = 0.0
            try:
                out_ram = subprocess.check_output("free -m", shell=True, text=True)
                lines = out_ram.strip().split('\n')
                parts = lines[1].split()
                total_ram_mb = int(parts[1])
                available_ram_mb = int(parts[6]) if len(parts) >= 7 else int(parts[3])
                used_ram_mb = total_ram_mb - available_ram_mb
                ram_percent = round((used_ram_mb / total_ram_mb) * 100, 1) if total_ram_mb > 0 else 0.0
                ram_used_gb = round(used_ram_mb / 1024, 1)
                ram_total_gb = round(total_ram_mb / 1024, 1)
                ram_str = f"{ram_used_gb}GiB / {ram_total_gb}GiB"
            except Exception:
                pass

            # 5. Get GPU VRAM
            vram_str = '未知'
            vram_used = 0
            vram_total = 0
            vram_percent = 0.0
            gpu_name = "NVIDIA GPU"
            try:
                try:
                    cmd_gpu_name = "nvidia-smi --query-gpu=name --format=csv,noheader"
                    gpu_name = subprocess.check_output(cmd_gpu_name, shell=True, text=True).strip()
                except Exception:
                    gpu_name = "NVIDIA GeForce GTX 1060 (模擬)"

                cmd_vram = "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits"
                out_vram = subprocess.check_output(cmd_vram, shell=True, text=True).strip()
                if out_vram:
                    parts = [p.strip() for p in out_vram.split(',')]
                    if len(parts) >= 2:
                        vram_used = int(parts[0])
                        vram_total = int(parts[1])
                        vram_percent = round((vram_used / vram_total) * 100, 1) if vram_total > 0 else 0.0
                        vram_str = f"{vram_used}MB / {vram_total}MB"
            except Exception:
                vram_str = "0MB / 6144MB"
                vram_total = 6144

            # Update cache
            SYSTEM_METRICS_CACHE.update({
                'model': model_name,
                'ram': ram_str,
                'vram': vram_str,
                'vram_used': vram_used,
                'vram_total': vram_total,
                'vram_percent': vram_percent,
                'cpu_percent': cpu_percent,
                'ram_used_gb': ram_used_gb,
                'ram_total_gb': ram_total_gb,
                'ram_percent': ram_percent,
                'gpu_name': gpu_name,
                'status': status,
                'status_color': status_color
            })
        except Exception as e:
            logger.error(f"Error in background system metrics thread: {e}")

        SYSTEM_METRICS_EVENT.wait(3.0)

def start_metrics_worker(app):
    """僅在主程序中啟動背景指標監控執行緒"""
    is_reloader = os.environ.get('USE_RELOADER') or app.debug
    should_start_thread = True
    if is_reloader and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        should_start_thread = False

    if should_start_thread:
        metrics_thread = threading.Thread(target=_update_system_metrics_worker, daemon=True)
        metrics_thread.start()
        
        def cleanup_background_threads():
            logger.info("Stopping background system metrics thread...")
            SYSTEM_METRICS_EVENT.clear()
            
        atexit.register(cleanup_background_threads)
    else:
        logger.info("Skipping background metrics thread initialization in Werkzeug parent process.")
