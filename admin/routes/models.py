import os
import time
import json
import threading
import subprocess
import requests as req_lib
from datetime import datetime, timezone, timedelta
from flask import render_template, request, jsonify, Response

from config import login_required, logger, get_server_metrics
from services.model_manager import (
    DOWNLOAD_STATUS, SWITCH_STATUS,
    _download_model_worker, _get_model_size_billion,
    _is_model_suitable, _is_file_suitable,
    _switch_model_worker, wait_for_llama_vram_clear
)

def _delete_temp_file_for_task(task_id: str):
    """Safely delete the temporary file (.tmp) associated with a failed download task."""
    try:
        filename = task_id.split('/')[-1]
        temp_path = os.path.join("/home/pipadmin/文件/models", filename + ".tmp")
        if os.path.exists(temp_path):
            os.remove(temp_path)
            logger.info({"msg": "Temporary model file removed successfully", "path": temp_path})
    except Exception as e:
        logger.error({"msg": "Failed to remove temporary model file", "task_id": task_id, "error": str(e)})

def register_models_routes(app):
    
    @app.route('/models/explore')
    @login_required
    def models_explore():
        # 獲取本地已下載的 GGUF 模型列表
        model_dir = "/home/pipadmin/文件/models"
        local_models = []
        if os.path.exists(model_dir):
            for f in os.listdir(model_dir):
                if f.endswith('.gguf'):
                    fpath = os.path.join(model_dir, f)
                    size_gb = round(os.path.getsize(fpath) / (1024*1024*1024), 2)
                    local_models.append({
                        'name': f,
                        'size': f"{size_gb} GB",
                        'path': fpath
                    })
        
        # 獲取目前啟動的模型名稱
        current_metrics = get_server_metrics()
        current_model = current_metrics.get('model', '無')
        
        return render_template('models_explore.html', local_models=local_models, current_model=current_model)


    @app.route('/api/models/local')
    @login_required
    def api_models_local():
        model_dir = "/home/pipadmin/文件/models"
        local_models = []
        if os.path.exists(model_dir):
            for f in os.listdir(model_dir):
                if f.endswith('.gguf'):
                    fpath = os.path.join(model_dir, f)
                    try:
                        size_gb = round(os.path.getsize(fpath) / (1024*1024*1024), 2)
                        local_models.append({
                            'name': f,
                            'size': f"{size_gb} GB",
                            'path': fpath
                        })
                    except Exception:
                        pass
                        
        current_metrics = get_server_metrics()
        current_model = current_metrics.get('model', '無')
        
        return jsonify({
            'local_models': local_models,
            'current_model': current_model
        })


    @app.route('/api/models/search')
    @login_required
    def api_models_search():
        import re
        query = request.args.get('q', '').strip()
        sort_by = request.args.get('sort', 'lastModified').strip()
        if sort_by not in ['lastModified', 'downloads', 'likes']:
            sort_by = 'lastModified'
        
        # 1. 取得 GPU 總顯存大小 (VRAM) 以進行動態篩選
        vram_total_gb = 0.0
        try:
            cmd_vram = "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits"
            out_vram = subprocess.check_output(cmd_vram, shell=True, text=True).strip()
            if out_vram:
                vram_total_gb = float(out_vram) / 1024.0
        except Exception:
            pass  # 無 GPU 或獲取失敗，視為純 CPU 模式
            
        # 智慧關鍵字分詞提取
        search_query = query
        q_parts = []
        if query:
            # 拆解成單字與數字部分 (小寫)
            q_parts = [p.lower() for p in re.findall(r'[a-zA-Z0-9\.]+', query) if p]
            # 尋找第一個主要英文單字（長度 >= 3 的純英文字母）
            alpha_parts = [p for p in q_parts if p.isalpha() and len(p) >= 3]
            if alpha_parts:
                search_query = alpha_parts[0]
                logger.info({"msg": "Optimized HF search query", "original": query, "optimized": search_query})

        # 擴大拉取數量至 100 名以供後端過濾，並使用 full=true 取得更新時間與檔案列表
        url = f"https://huggingface.co/api/models?sort={sort_by}&direction=-1&limit=100&filter=gguf&full=true"
        if search_query:
            url += f"&search={search_query}"
            
        try:
            resp = req_lib.get(url, timeout=10)
            resp.raise_for_status()
            models = resp.json()
            
            processed_models = []
            for m in models:
                model_id = m.get('id', '')
                downloads = m.get('downloads', 0)
                likes = m.get('likes', 0)
                
                # 1. 排除過於陳舊的模型 (只保留 2024 年之後更新的活躍模型)
                raw_date_str = m.get('lastModified') or m.get('createdAt')
                updated_at = "1970-01-01"
                if raw_date_str:
                    try:
                        # Parse ISO-8601 string (e.g., 2026-06-16T12:48:20.000Z)
                        clean_str = raw_date_str.replace('Z', '+00:00')
                        dt = datetime.fromisoformat(clean_str)
                        
                        # Convert to Taipei timezone (UTC+8)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        taipei_tz = timezone(timedelta(hours=8))
                        dt_taipei = dt.astimezone(taipei_tz)
                        
                        if dt_taipei.year < 2024:
                            continue  # Skip obsolete models before 2024
                        updated_at = dt_taipei.strftime("%Y-%m-%d")
                    except Exception as ex:
                        # Log error with structured logging (Rule #7)
                        logger.error(
                            {"model_id": model_id, "raw_date": raw_date_str, "error": str(ex)},
                            "Failed to parse model updated_at date"
                        )
                        # Fallback to basic string slicing
                        try:
                            updated_at = raw_date_str[:10]
                        except Exception:
                            pass
                
                # 2. 解析模型參數大小 (Billion)
                model_size_b = _get_model_size_billion(model_id)
                
                # 3. 依據本機配備之 VRAM 動態篩選適合運行的模型大小
                suitable = _is_model_suitable(model_size_b, vram_total_gb)
                    
                # 計算該 Repository 內的 GGUF 檔案數量，若無 GGUF 檔則過濾
                siblings = m.get('siblings', [])
                gguf_count = sum(1 for s in siblings if s.get('rfilename', '').endswith('.gguf'))
                if gguf_count == 0:
                    continue
                
                # 4. 本地 Regex 重新評分重新排序 (Rerank)
                match_score = 0.0
                if query and q_parts:
                    model_id_lower = model_id.lower()
                    for part in q_parts:
                        if part in model_id_lower:
                            # 基礎加分
                            match_score += 10.0
                            # 核心關鍵特徵加分 (如 35b, moe, mtp 等)
                            if part in ['moe', 'mtp', 'gguf', 'uncensored'] or (part.endswith('b') and part[:-1].replace('.', '', 1).isdigit()):
                                match_score += 15.0
                
                processed_models.append({
                    'id': model_id,
                    'downloads': downloads,
                    'likes': likes,
                    'suitable': suitable,
                    'updated_at': updated_at,
                    'gguf_count': gguf_count,
                    'match_score': match_score
                })
                
            # 如果有搜尋字詞，優先按 match_score 降序排序，其次按 sort_by 排序
            if query:
                processed_models.sort(key=lambda x: (-x['match_score'], -x[sort_by] if sort_by != 'lastModified' else x['id']))
                
            # 最多只回傳前 25 個精選模型以防頁面過長
            return jsonify(processed_models[:25])
        except Exception as e:
            logger.error(f"Search Hugging Face models failed: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/models/files')
    @login_required
    def api_models_files():
        model_id = request.args.get('model_id', '').strip()
        if not model_id:
            return jsonify({'error': '缺少 model_id'}), 400
            
        # 取得本機 GPU 總顯存大小 (VRAM)
        vram_total_gb = 0.0
        try:
            cmd_vram = "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits"
            out_vram = subprocess.check_output(cmd_vram, shell=True, text=True).strip()
            if out_vram:
                vram_total_gb = float(out_vram) / 1024.0
        except Exception:
            pass
            
        # 優先取得當前最新的 commit sha 作為 pointer，完美支援 master/main 或其他 default branch 名稱
        branch_or_sha = "main"
        try:
            detail_url = f"https://huggingface.co/api/models/{model_id}"
            detail_resp = req_lib.get(detail_url, timeout=5)
            if detail_resp.status_code == 200:
                branch_or_sha = detail_resp.json().get('sha', 'main')
        except Exception:
            pass
            
        url = f"https://huggingface.co/api/models/{model_id}/tree/{branch_or_sha}"
        try:
            resp = req_lib.get(url, timeout=10)
            resp.raise_for_status()
            files = resp.json()
            
            gguf_files = []
            for f in files:
                path = f.get('path', '')
                size = f.get('size', 0)
                if path.endswith('.gguf'):
                    suitable = _is_file_suitable(size, vram_total_gb, filename=path)
                    gguf_files.append({
                        'name': path,
                        'size_formatted': f"{round(size / (1024*1024*1024), 2)} GB" if size else "未知",
                        'suitable': suitable
                    })
            return jsonify(gguf_files)
        except Exception as e:
            logger.error(f"Fetch Hugging Face files failed: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/models/download', methods=['POST'])
    @login_required
    def api_models_download():
        try:
            data = request.get_json() or {}
            model_id = data.get('model_id', '').strip()
            filename = data.get('filename', '').strip()
            
            if not model_id or not filename:
                return jsonify({'error': '缺少必要引數'}), 400
                
            # 防止路徑穿越，清理檔名
            filename = os.path.basename(filename)
            save_path = os.path.join("/home/pipadmin/文件/models", filename)
            
            # 拼接 HF 的下載 URL
            download_url = f"https://huggingface.co/{model_id}/resolve/main/{filename}"
            
            # 檢查是否已下載或正在下載
            if os.path.exists(save_path):
                return jsonify({'error': '該模型檔案已下載存在於 models/ 中'}), 400
                
            status_key = f"{model_id}/{filename}"
            if status_key in DOWNLOAD_STATUS and DOWNLOAD_STATUS[status_key]["status"] == "downloading":
                return jsonify({'error': '該模型正在下載中'}), 400
                
            # 開啟非同步執行緒下載
            t = threading.Thread(target=_download_model_worker, args=(status_key, download_url, save_path))
            t.start()
            
            return jsonify({'ok': True, 'msg': '已開始在背景下載模型，請至進度板查看。', 'task_id': status_key})
        except Exception as e:
            logger.error(f"Trigger model download failed: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/models/download/status')
    @login_required
    def api_models_download_status():
        task_id = request.args.get('task_id', '').strip()
        if task_id:
            return jsonify(DOWNLOAD_STATUS.get(task_id, {'status': 'not_found'}))
        return jsonify(DOWNLOAD_STATUS)


    @app.route('/api/models/download/clear', methods=['DELETE'])
    @login_required
    def api_models_download_clear():
        """Clear completed/failed download tasks from the status board.

        - DELETE /api/models/download/clear          → clear all non-downloading tasks
        - DELETE /api/models/download/clear?task_id=X → clear single task (refused if downloading)
        """
        task_id = request.args.get('task_id', '').strip()

        if task_id:
            task = DOWNLOAD_STATUS.get(task_id)
            if task is None:
                return jsonify({'error': '找不到該任務'}), 404
            if task.get('status') == 'downloading':
                del DOWNLOAD_STATUS[task_id]
                logger.info({"msg": "download task cancelled", "task_id": task_id})
                return jsonify({'ok': True, 'msg': '已取消下載任務'})
            
            # If the task failed, clean up its temporary file
            if task.get('status') == 'failed':
                _delete_temp_file_for_task(task_id)

            del DOWNLOAD_STATUS[task_id]
            logger.info({"msg": "download task cleared", "task_id": task_id})
            return jsonify({'ok': True, 'msg': '已清除任務'})

        # Clear all finished tasks (completed or failed)
        finished = [k for k, v in DOWNLOAD_STATUS.items() if v.get('status') != 'downloading']
        for k in finished:
            task = DOWNLOAD_STATUS[k]
            if task.get('status') == 'failed':
                _delete_temp_file_for_task(k)
            del DOWNLOAD_STATUS[k]
        logger.info({"msg": "download tasks cleared", "count": len(finished)})
        return jsonify({'ok': True, 'msg': f'已清除 {len(finished)} 筆已完成/失敗任務'})


    @app.route('/api/models/switch', methods=['POST'])
    @login_required
    def api_models_switch():
        try:
            if SWITCH_STATUS['status'] == 'switching':
                return jsonify({'error': f'目前已有模型 ({SWITCH_STATUS["model_name"]}) 正在切換中，請稍候。'}), 400

            data = request.get_json() or {}
            model_name = data.get('model_name', '').strip()
            if not model_name or not model_name.endswith('.gguf'):
                return jsonify({'error': '無效的模型名稱'}), 400
                
            model_name = os.path.basename(model_name)
            model_dir = "/home/pipadmin/文件/models"
            selected_path = os.path.join(model_dir, model_name)
            
            if not os.path.exists(selected_path):
                return jsonify({'error': '該模型檔案不存在'}), 400
                
            config_dir = os.path.expanduser("~/.config/linebot")
            os.makedirs(config_dir, exist_ok=True)
            selected_model_file = os.path.join(config_dir, "selected_model")
            
            # 備份舊的模型路徑以備回滾
            old_selected_path = None
            if os.path.exists(selected_model_file):
                try:
                    with open(selected_model_file, "r") as sf:
                        old_selected_path = sf.read().strip()
                except Exception:
                    pass
                    
            # 讀取微調設定檔以備份舊設定 (回滾用)
            config_path = os.path.join(config_dir, "engine_config.json")
            old_cfg = {'threads': 8, 'gpu_layers': 10, 'ctx_size': 8192}
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r') as cf:
                        old_cfg = json.load(cf)
                except Exception:
                    pass

            # 根據目標模型自動配置最佳硬體參數
            m_name_lower = model_name.lower()
            file_size_gb = 0
            if os.path.exists(selected_path):
                file_size_gb = os.path.getsize(selected_path) / (1024 * 1024 * 1024)
                
            # 預設分級
            model_class = "large"
            if "mai_base" in m_name_lower:
                model_class = "mai_base"
            elif any(kw in m_name_lower for kw in ["gemma-4", "4b", "3b", "2b", "gemma2-2b", "lfm", "a1b", "nemotron"]) or (0.1 <= file_size_gb < 4.8):
                model_class = "tiny"
            elif any(kw in m_name_lower for kw in ["8b", "7b", "9b", "gemma2-9b"]):
                model_class = "medium"
            elif 0.1 <= file_size_gb < 7.5:
                model_class = "medium"

            if model_class == "mai_base":
                threads = 6
                gpu_layers = 99
                ctx_size = 4096
            elif model_class == "tiny":
                threads = 6
                gpu_layers = 99
                ctx_size = 4096
            elif model_class == "medium":
                threads = 6
                gpu_layers = 24
                ctx_size = 4096
            else:
                threads = 6
                gpu_layers = 20
                ctx_size = 4096

            user_systemd = os.path.expanduser("~/.config/systemd/user")
            os.makedirs(user_systemd, exist_ok=True)
            service_path = os.path.join(user_systemd, "linebot-llama.service")
            
            # Start switch thread
            t = threading.Thread(
                target=_switch_model_worker,
                args=(selected_path, model_name, old_selected_path, old_cfg, threads, gpu_layers, ctx_size, selected_model_file, service_path, config_path)
            )
            t.start()
            
            return jsonify({'ok': True, 'msg': '已開始在背景切換模型，請稍候。', 'status': 'switching'})
        except Exception as e:
            logger.error(f"Switch model failed to trigger: {e}")
            return jsonify({'error': f'觸發切換失敗: {str(e)}'}), 500


    @app.route('/api/models/switch/status')
    @login_required
    def api_models_switch_status():
        return jsonify(SWITCH_STATUS)


    @app.route('/api/models/config', methods=['GET', 'POST'])
    @login_required
    def api_models_config():
        config_dir = os.path.expanduser("~/.config/linebot")
        config_path = os.path.join(config_dir, "engine_config.json")
        os.makedirs(config_dir, exist_ok=True)
        
        # 預設設定
        default_config = {
            'threads': 8,
            'gpu_layers': 10,
            'ctx_size': 8192
        }
        
        if request.method == 'GET':
            config = default_config.copy()
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r') as f:
                        user_config = json.load(f)
                        config.update(user_config)
                except Exception:
                    pass
            return jsonify(config)
            
        elif request.method == 'POST':
            try:
                data = request.get_json() or {}
                threads = int(data.get('threads', 8))
                gpu_layers = int(data.get('gpu_layers', 10))
                ctx_size = int(data.get('ctx_size', 8192))
                
                # 安全範圍檢查與防呆限制
                if threads < 1 or threads > 64:
                    threads = 8
                if gpu_layers < 0 or gpu_layers > 256:
                    gpu_layers = 10
                if ctx_size < 512 or ctx_size > 65536:
                    ctx_size = 8192
                    
                config = {
                    'threads': threads,
                    'gpu_layers': gpu_layers,
                    'ctx_size': ctx_size
                }
                
                # 備份舊配置以備回滾
                old_config = {'threads': 8, 'gpu_layers': 10, 'ctx_size': 8192}
                if os.path.exists(config_path):
                    try:
                        with open(config_path, 'r') as cf:
                            old_config = json.load(cf)
                    except Exception:
                        pass

                with open(config_path, 'w') as f:
                    json.dump(config, f)
                    
                # 如果當前有已啟用的模型，立刻重啟以套用新參數
                selected_model_path = os.path.join(config_dir, "selected_model")
                restarted = False
                if os.path.exists(selected_model_path):
                    with open(selected_model_path, 'r') as sf:
                        selected_path = sf.read().strip()
                    if os.path.exists(selected_path):
                        user_systemd = os.path.expanduser("~/.config/systemd/user")
                        service_path = os.path.join(user_systemd, "linebot-llama.service")
                        
                        def write_service_file(m_path, t, g, c):
                            extra_args = []
                            m_path_lower = m_path.lower()
                            # MoE models benefit from --cpu-moe to keep expert routing on CPU
                            if any(kw in m_path_lower for kw in ["moe", "a3b", "mixtral", "dbrx", "a1b", "lfm"]):
                                extra_args.append("--cpu-moe")
                            extra_args.append("--no-mmap")
                            extra_args.append("--mlock")  # Lock model weights in RAM to prevent swap
                            extra_args.append("--flash-attn auto")  # Let llama.cpp auto-detect FA support (requires Volta+)
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

                        write_service_file(selected_path, threads, gpu_layers, ctx_size)
                        
                        # 3. 重新載入 Systemd 並重啟服務
                        subprocess.run("systemctl --user daemon-reload", shell=True, check=True)
                        subprocess.run("systemctl --user stop linebot-llama", shell=True)
                        subprocess.run("pkill -9 -f 'llama-server'", shell=True)
                        wait_for_llama_vram_clear()
                        subprocess.run("systemctl --user start linebot-llama", shell=True, check=True)
                        restarted = True
                        
                        # 偵測是否崩潰
                        time.sleep(2.5)
                        status_check = subprocess.run("systemctl --user is-active linebot-llama", shell=True, capture_output=True, text=True)
                        is_active = status_check.stdout.strip() == "active"
                        
                        if not is_active:
                            err_msg = "新微調參數導致引擎啟動失敗。"
                            log_path = "/home/pipadmin/文件/llama.log"
                            if os.path.exists(log_path):
                                try:
                                    with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
                                        log_lines = lf.readlines()[-150:]
                                    log_text = "".join(log_lines)
                                    if "cudaError" in log_text or "CUDA error" in log_text or "out of memory" in log_text or "CUDA_ERROR_OUT_OF_MEMORY" in log_text:
                                        err_msg = "新微調參數導致顯卡記憶體 (VRAM) 不足！請調低 GPU 卸載層數或縮小上下文大小。"
                                except Exception:
                                    pass
                            
                            # 回滾到舊配置
                            with open(config_path, 'w') as f:
                                json.dump(old_config, f)
                            write_service_file(selected_path, old_config.get('threads', 8), old_config.get('gpu_layers', 10), old_config.get('ctx_size', 8192))
                            subprocess.run("systemctl --user daemon-reload", shell=True)
                            subprocess.run("systemctl --user stop linebot-llama", shell=True)
                            subprocess.run("pkill -9 -f 'llama-server'", shell=True)
                            wait_for_llama_vram_clear()
                            subprocess.run("systemctl --user start linebot-llama", shell=True)
                            return jsonify({'error': f'{err_msg} 已自動回滾至先前的微調配置。'}), 400
                            
                return jsonify({'ok': True, 'restarted': restarted, 'msg': '微調配置已成功儲存並套用！'})
            except Exception as e:
                logger.error(f"Save engine config failed: {e}")
                return jsonify({'error': f'儲存微調配置失敗: {str(e)}'}), 500


    @app.route('/api/models/delete', methods=['POST'])
    @login_required
    def delete_model():
        try:
            data = request.get_json() or {}
            model_name = data.get('model_name', '').strip()
            if not model_name:
                return jsonify({'error': '未提供模型名稱'}), 400
                
            # 安全性檢查
            if '/' in model_name or '\\' in model_name or '..' in model_name:
                return jsonify({'error': '非法檔案名稱'}), 400
                
            if not model_name.endswith('.gguf'):
                return jsonify({'error': '只能刪除 .gguf 模型檔案'}), 400

            # 檢查是否為當前啟用中的模型
            current_metrics = get_server_metrics()
            current_model = current_metrics.get('model', '無')
            current_model_name = os.path.basename(current_model)
            
            if model_name == current_model_name:
                return jsonify({'error': '無法刪除目前啟用中的模型。請先切換至其他模型後再行刪除。'}), 400
                
            MODELS_DIR = "/home/pipadmin/文件/models"
            target_path = os.path.join(MODELS_DIR, model_name)
            
            if not os.path.exists(target_path):
                return jsonify({'error': '模型檔案不存在'}), 404
                
            os.remove(target_path)
            logger.info(f"Model deleted: {model_name}")
            return jsonify({'ok': True, 'msg': f'模型 {model_name} 已成功刪除，釋放硬碟空間！'})
        except Exception as e:
            logger.error(f"Delete model failed: {e}")
            return jsonify({'error': f'刪除失敗: {str(e)}'}), 500
