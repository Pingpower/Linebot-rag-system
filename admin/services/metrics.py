import os
import subprocess
import time
import requests as req_lib
import threading
import atexit
import re
from config import SYSTEM_METRICS_EVENT, SYSTEM_METRICS_CACHE, logger
import config

# Monkey patch config.get_server_metrics to include detailed GPU metrics
_original_get_server_metrics = config.get_server_metrics

def custom_get_server_metrics():
    global SYSTEM_METRICS_CACHE
    metrics = _original_get_server_metrics()
    metrics.update({
        'gpu_name': SYSTEM_METRICS_CACHE.get('gpu_name', '未知'),
        'vram_total': SYSTEM_METRICS_CACHE.get('vram_total', 0),
        'vram_used': SYSTEM_METRICS_CACHE.get('vram_used', 0),
        'vram_percent': SYSTEM_METRICS_CACHE.get('vram_percent', 0.0),
        'cpu_percent': SYSTEM_METRICS_CACHE.get('cpu_percent', 0.0),
        'ram_used_gb': SYSTEM_METRICS_CACHE.get('ram_used_gb', 0.0),
        'ram_total_gb': SYSTEM_METRICS_CACHE.get('ram_total_gb', 0.0),
        'ram_percent': SYSTEM_METRICS_CACHE.get('ram_percent', 0.0)
    })
    return metrics

# Apply the monkey patch globally
config.get_server_metrics = custom_get_server_metrics
get_server_metrics = custom_get_server_metrics

def detect_gpu_hardware_info():
    """Detect GPU hardware name and total VRAM size in MB."""
    gpu_name = "NVIDIA GPU"
    vram_total = 0
    nvidia_smi_success = False

    try:
        # Try nvidia-smi safely without shell=True
        gpu_name = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True, timeout=5.0
        ).strip()
        
        out_vram = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True, timeout=5.0
        ).strip()
        
        if out_vram:
            vram_total = int(out_vram)
            nvidia_smi_success = True
    except Exception:
        pass

    if not nvidia_smi_success:
        # nvidia-smi failed, try lspci fallback mechanism
        try:
            out_lspci = subprocess.check_output(["lspci"], text=True, timeout=5.0)
            nvidia_line = ""
            for line in out_lspci.splitlines():
                if "nvidia" in line.lower() or "geforce" in line.lower():
                    nvidia_line = line
                    break
            
            if nvidia_line:
                # Extract GPU model name
                name_match = re.search(r'\[([^\]]+)\]', nvidia_line)
                if name_match:
                    gpu_name = name_match.group(1)
                else:
                    idx = nvidia_line.lower().find("nvidia corporation")
                    if idx != -1:
                        gpu_name = nvidia_line[idx + len("nvidia corporation"):].strip()
                    else:
                        gpu_name = "NVIDIA GPU"
                
                # Match with common NVIDIA GPU database (VRAM size in MB)
                gpu_db = {
                    "4090": 24576,
                    "4080": 16384,
                    "4070 ti": 12288,
                    "4070": 12288,
                    "4060 ti": 16384,
                    "4060": 8192,
                    "3090": 24576,
                    "3080 ti": 12288,
                    "3080": 10240,
                    "3070 ti": 8192,
                    "3070": 8192,
                    "3060 ti": 8192,
                    "3060": 12288,
                    "2080 ti": 11264,
                    "2080": 8192,
                    "2070": 8192,
                    "2060": 6144,
                    "1080 ti": 11264,
                    "1080": 8192,
                    "1070": 8192,
                    "1060": 6144,
                    "1660": 6144,
                    "1650": 4096,
                    "t4": 16384,
                    "a100": 40960,
                    "a10g": 24576,
                    "l4": 24576
                }
                
                vram_total = 6144  # Fallback GTX 1060 (6GB)
                gpu_name_lower = gpu_name.lower()
                for key, val in gpu_db.items():
                    if key in gpu_name_lower:
                        vram_total = val
                        break
            else:
                gpu_name = "CPU 模式"
                vram_total = 0
        except Exception as ex:
            logger.error(f"Error in lspci fallback: {ex}")
            gpu_name = "CPU 模式"
            vram_total = 0

    return gpu_name, vram_total

# Initialize GPU hardware info synchronously on module load (cold-start optimization)
try:
    _init_gpu_name, _init_vram_total = detect_gpu_hardware_info()
    SYSTEM_METRICS_CACHE.update({
        'gpu_name': _init_gpu_name,
        'vram_total': _init_vram_total,
        'vram_used': 0,
        'vram_percent': 0.0,
        'vram': f"0MB / {_init_vram_total}MB" if _init_vram_total > 0 else "0MB / 0MB"
    })
except Exception as _init_ex:
    logger.error(f"Error during synchronous metrics initialization: {_init_ex}")

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
            # 1. Detect LLaMA engine status (Safe subprocess call, avoiding shell=True)
            try:
                cmd_status = ["systemctl", "--user", "is-active", "linebot-llama"]
                sys_status = subprocess.check_output(cmd_status, text=True, timeout=5.0).strip()
            except subprocess.CalledProcessError as e:
                sys_status = e.output.strip() if e.output else "inactive"
            except Exception:
                sys_status = "inactive"

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

            # 2. Get active model name (Safely scan /proc cmdline to avoid shell=True & grep)
            model_name = '無 / 未啟動'
            try:
                found_model = None
                for pid_dir in os.listdir('/proc'):
                    if pid_dir.isdigit():
                        try:
                            with open(os.path.join('/proc', pid_dir, 'cmdline'), 'rb') as f:
                                cmdline = f.read().split(b'\x00')
                            cmd_parts = [p.decode('utf-8', errors='ignore') for p in cmdline if p]
                            is_llama = False
                            for part in cmd_parts:
                                if 'llama-server' in part:
                                    is_llama = True
                                    break
                            if is_llama:
                                for i, part in enumerate(cmd_parts):
                                    if part == '--model' and i + 1 < len(cmd_parts):
                                        found_model = cmd_parts[i+1]
                                        break
                                if found_model:
                                    break
                        except Exception:
                            continue
                if found_model:
                    model_name = os.path.basename(found_model)
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

            # 4. Get System RAM (Safe subprocess call)
            ram_str = '未知'
            ram_used_gb = 0.0
            ram_total_gb = 0.0
            ram_percent = 0.0
            try:
                out_ram = subprocess.check_output(["free", "-m"], text=True, timeout=5.0)
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

            # 5. Get GPU VRAM using detect_gpu_hardware_info()
            vram_str = '未知'
            vram_used = 0
            vram_total = 0
            vram_percent = 0.0
            gpu_name, vram_total = detect_gpu_hardware_info()
            
            if vram_total > 0:
                nvidia_smi_success = False
                try:
                    # Try to query VRAM usage
                    out_used = subprocess.check_output(
                        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                        text=True, timeout=5.0
                    ).strip()
                    if out_used:
                        vram_used = int(out_used)
                        nvidia_smi_success = True
                except Exception:
                    pass

                if not nvidia_smi_success:
                    # Estimate VRAM usage based on running model file size if active
                    if sys_status == "active" and model_name != '無 / 未啟動':
                        model_dir = "/home/pipadmin/文件/models"
                        model_path = os.path.join(model_dir, model_name)
                        if os.path.exists(model_path):
                            file_size_mb = int(os.path.getsize(model_path) / (1024 * 1024))
                            vram_used = min(file_size_mb, vram_total - 500)
                            vram_used = max(vram_used, 0)
                
                vram_percent = round((vram_used / vram_total) * 100, 1) if vram_total > 0 else 0.0
                vram_str = f"{vram_used}MB / {vram_total}MB"
            else:
                vram_str = "0MB / 0MB"

            # Update global system metrics cache
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
    """Start background system metrics worker thread inside the Flask application context."""
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