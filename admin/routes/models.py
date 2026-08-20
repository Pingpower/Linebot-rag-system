import os
import time
import json
import threading
import subprocess
import requests as req_lib
from datetime import datetime, timezone, timedelta
from flask import render_template, request, jsonify, Response
from collections import deque

from config import login_required, logger
import config

def get_server_metrics():
    """Wrapper to dynamically access monkey-patched server metrics from config."""
    return config.get_server_metrics()

from services.model_manager import (
    DOWNLOAD_STATUS, SWITCH_STATUS, model_switch_lock,
    _download_model_worker, _get_model_size_billion,
    _is_model_suitable, _is_file_suitable,
    _switch_model_worker, wait_for_llama_vram_clear,
    _config_apply_worker, _read_last_log_lines_optimized,
    _write_llama_service_file
)

# Global thread locks to protect shared file writes
hf_cache_lock = threading.Lock()

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
        # Get list of locally downloaded GGUF models
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
        
        # Get currently active model name
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
        
        # 1. HF search result cache mechanism (30 minutes expiry) with Thread Lock
        cache_key = f"{query}_{sort_by}"
        CACHE_FILE = "/home/pipadmin/文件/admin/hf_search_cache.json"
        
        cached_data = None
        with hf_cache_lock:
            if os.path.exists(CACHE_FILE):
                try:
                    with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                        cache = json.load(f)
                    entry = cache.get(cache_key)
                    if entry:
                        timestamp = entry.get('timestamp', 0)
                        if time.time() - timestamp < 1800:  # 30 mins
                            cached_data = entry.get('data')
                except Exception as ex:
                    logger.warning(f"Failed to read HF search cache: {ex}")
                
        if cached_data is not None:
            logger.info(f"HF search cache HIT for key: {cache_key}")
            return jsonify(cached_data)

        logger.info(f"HF search cache MISS for key: {cache_key}. Fetching from HF API...")

        # 2. Directly read total VRAM size (GB) from get_server_metrics()
        current_metrics = get_server_metrics()
        vram_total_gb = current_metrics.get('vram_total', 0) / 1024.0
            
        # Smart query parsing to support author/model format
        author_query = ""
        search_query = query.strip()
        if query and '/' in query:
            parts = query.split('/', 1)
            author_query = parts[0].strip()
            search_query = parts[1].strip()
            
        q_parts = []
        if query:
            q_parts = [p.lower() for p in re.findall(r'[a-zA-Z0-9\.]+', query) if p]

        url = f"https://huggingface.co/api/models?sort={sort_by}&direction=-1&limit=100&filter=gguf&full=true"
        if author_query:
            url += f"&author={author_query}"
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
                
                # Exclude obsolete models modified before 2024
                raw_date_str = m.get('lastModified') or m.get('createdAt')
                updated_at = "1970-01-01"
                updated_timestamp = 0.0
                if raw_date_str:
                    try:
                        clean_str = raw_date_str.replace('Z', '+00:00')
                        dt = datetime.fromisoformat(clean_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        updated_timestamp = dt.timestamp()
                        
                        taipei_tz = timezone(timedelta(hours=8))
                        dt_taipei = dt.astimezone(taipei_tz)
                        
                        if dt_taipei.year < 2024:
                            continue
                        updated_at = dt_taipei.strftime("%Y-%m-%d")
                    except Exception as ex:
                        logger.error(
                            {"model_id": model_id, "raw_date": raw_date_str, "error": str(ex)},
                            "Failed to parse model updated_at date"
                        )
                        try:
                            updated_at = raw_date_str[:10]
                        except Exception:
                            pass
                
                model_size_b = _get_model_size_billion(model_id)
                suitable = _is_model_suitable(model_size_b, vram_total_gb)
                    
                # Skip repos containing no GGUF files
                siblings = m.get('siblings', [])
                gguf_count = sum(1 for s in siblings if s.get('rfilename', '').endswith('.gguf'))
                if gguf_count == 0:
                    continue
                
                # Dynamic matching score for reranking
                match_score = 0.0
                if query and q_parts:
                    model_id_lower = model_id.lower()
                    for part in q_parts:
                        if part in model_id_lower:
                            match_score += 10.0
                            if part in ['moe', 'mtp', 'gguf', 'uncensored'] or (part.endswith('b') and part[:-1].replace('.', '', 1).isdigit()):
                                match_score += 15.0
                
                processed_models.append({
                    'id': model_id,
                    'downloads': downloads,
                    'likes': likes,
                    'suitable': suitable,
                    'updated_at': updated_at,
                    'updated_timestamp': updated_timestamp,
                    'gguf_count': gguf_count,
                    'match_score': match_score
                })
                
            # Precision sorting based on sort_by
            sort_key_map = {
                'downloads': 'downloads',
                'likes': 'likes',
                'lastModified': 'updated_timestamp'
            }
            target_sort_key = sort_key_map.get(sort_by, 'updated_timestamp')
            
            # Sort by match_score desc, sort key desc, and id asc
            processed_models.sort(key=lambda x: (-x['match_score'], -x[target_sort_key], x['id']))
                
            result_list = processed_models[:25]
            
            # Atomic update search cache file with Thread Lock (LRU limited to 100 entries)
            try:
                with hf_cache_lock:
                    cache = {}
                    if os.path.exists(CACHE_FILE):
                        try:
                            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                                cache = json.load(f)
                        except Exception:
                            pass
                            
                    cache[cache_key] = {
                        'timestamp': time.time(),
                        'data': result_list
                    }
                    
                    # Enforce max 100 entries by removing the oldest ones
                    if len(cache) > 100:
                        sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get('timestamp', 0))
                        keys_to_remove = sorted_keys[:len(cache) - 100]
                        for k in keys_to_remove:
                            cache.pop(k, None)
                            
                    temp_cache = CACHE_FILE + ".tmp"
                    with open(temp_cache, 'w', encoding='utf-8') as f:
                        json.dump(cache, f, ensure_ascii=False, indent=2)
                    os.replace(temp_cache, CACHE_FILE)
            except Exception as cache_ex:
                logger.warning(f"Failed to write search results to cache: {cache_ex}")

            return jsonify(result_list)
        except Exception as e:
            logger.error(f"Search Hugging Face models failed: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/models/files')
    @login_required
    def api_models_files():
        model_id = request.args.get('model_id', '').strip()
        if not model_id:
            return jsonify({'error': '缺少 model_id'}), 400
            
        current_metrics = get_server_metrics()
        vram_total_gb = current_metrics.get('vram_total', 0) / 1024.0
            
        # Streamline redundant API calls by directly pointing to "main"
        branch_or_sha = "main"
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
                
            # Prevent path traversal
            filename = os.path.basename(filename)
            save_path = os.path.join("/home/pipadmin/文件/models", filename)
            download_url = f"https://huggingface.co/{model_id}/resolve/main/{filename}"
            
            if os.path.exists(save_path):
                return jsonify({'error': '該模型檔案已下載存在於 models/ 中'}), 400
                
            status_key = f"{model_id}/{filename}"
            if status_key in DOWNLOAD_STATUS and DOWNLOAD_STATUS[status_key]["status"] == "downloading":
                return jsonify({'error': '該模型正在下載中'}), 400
                
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
        task_id = request.args.get('task_id', '').strip()

        if task_id:
            task = DOWNLOAD_STATUS.get(task_id)
            if task is None:
                return jsonify({'error': '找不到該任務'}), 404
            if task.get('status') == 'downloading':
                DOWNLOAD_STATUS.pop(task_id, None)
                logger.info({"msg": "download task cancelled", "task_id": task_id})
                return jsonify({'ok': True, 'msg': '已取消下載任務'})
            
            if task.get('status') == 'failed':
                _delete_temp_file_for_task(task_id)

            DOWNLOAD_STATUS.pop(task_id, None)
            logger.info({"msg": "download task cleared", "task_id": task_id})
            return jsonify({'ok': True, 'msg': '已清除任務'})

        finished = [k for k, v in DOWNLOAD_STATUS.items() if v.get('status') != 'downloading']
        for k in finished:
            task = DOWNLOAD_STATUS[k]
            if task.get('status') == 'failed':
                _delete_temp_file_for_task(k)
            DOWNLOAD_STATUS.pop(k, None)
        logger.info({"msg": "download tasks cleared", "count": len(finished)})
        return jsonify({'ok': True, 'msg': f'已清除 {len(finished)} 筆已完成/失敗任務'})


    @app.route('/api/models/switch', methods=['POST'])
    @login_required
    def api_models_switch():
        try:
            if SWITCH_STATUS['status'] == 'switching':
                return jsonify({'error': f'目前已有模型 ({SWITCH_STATUS["model_name"]}) 正在切換中，請稍候。'}), 400

            # Acquire global lock to prevent race conditions during model switch requests
            acquired = model_switch_lock.acquire(blocking=False)
            if not acquired:
                return jsonify({'error': '目前已有模型正在切換中，請稍候。'}), 400

            data = request.get_json() or {}
            model_name = data.get('model_name', '').strip()
            if not model_name or not model_name.endswith('.gguf'):
                try:
                    model_switch_lock.release()
                except RuntimeError:
                    pass
                return jsonify({'error': '無效的模型名稱'}), 400
                
            model_name = os.path.basename(model_name)
            model_dir = "/home/pipadmin/文件/models"
            selected_path = os.path.join(model_dir, model_name)
            
            if not os.path.exists(selected_path):
                try:
                    model_switch_lock.release()
                except RuntimeError:
                    pass
                return jsonify({'error': '該模型檔案不存在'}), 400
                
            config_dir = os.path.expanduser("~/.config/linebot")
            os.makedirs(config_dir, exist_ok=True)
            selected_model_file = os.path.join(config_dir, "selected_model")
            
            # Backup old selected path for potential rollback
            old_selected_path = None
            if os.path.exists(selected_model_file):
                try:
                    with open(selected_model_file, "r") as sf:
                        old_selected_path = sf.read().strip()
                except Exception:
                    pass
                    
            # Prioritize existing user-tuned parameters (avoid overriding them dynamically)
            config_path = os.path.join(config_dir, "engine_config.json")
            old_cfg = {'threads': 8, 'gpu_layers': 10, 'ctx_size': 8192}
            has_custom_config = False
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r') as cf:
                        old_cfg = json.load(cf)
                        if 'threads' in old_cfg and 'gpu_layers' in old_cfg and 'ctx_size' in old_cfg:
                            has_custom_config = True
                except Exception:
                    pass

            if has_custom_config:
                threads = old_cfg['threads']
                gpu_layers = old_cfg['gpu_layers']
                ctx_size = old_cfg['ctx_size']
                logger.info(f"Retaining user customized config: threads={threads}, gpu_layers={gpu_layers}, ctx_size={ctx_size}")
            else:
                # Set to None to trigger optimal hardware parameter estimation inside _switch_model_worker
                threads = None
                gpu_layers = None
                ctx_size = None
                logger.info("No custom config detected. Fallback to dynamic hardware resource estimation.")

            user_systemd = os.path.expanduser("~/.config/systemd/user")
            os.makedirs(user_systemd, exist_ok=True)
            service_path = os.path.join(user_systemd, "linebot-llama.service")
            
            # Start background switch thread (lock is released inside the worker's finally block)
            t = threading.Thread(
                target=_switch_model_worker,
                args=(selected_path, model_name, old_selected_path, old_cfg, threads, gpu_layers, ctx_size, selected_model_file, service_path, config_path)
            )
            t.start()
            
            return jsonify({'ok': True, 'msg': '已開始在背景切換模型，請稍候。', 'status': 'switching'})
        except Exception as e:
            logger.error(f"Switch model failed to trigger: {e}")
            try:
                model_switch_lock.release()
            except RuntimeError:
                pass
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
        
        default_config = {
            'threads': 8,
            'gpu_layers': 10,
            'ctx_size': 8192
        }
        
        if request.method == 'GET':
            config_data = default_config.copy()
            if os.path.exists(config_path):
                try:
                    with open(config_path, 'r') as f:
                        user_config = json.load(f)
                        config_data.update(user_config)
                except Exception:
                    pass
            return jsonify(config_data)
            
        elif request.method == 'POST':
            # Acquire global lock to prevent race conditions during config apply or model switch requests
            acquired = model_switch_lock.acquire(blocking=False)
            if not acquired:
                return jsonify({'error': '系統目前正忙於切換模型或調整配置，請稍候。'}), 400
            
            try:
                data = request.get_json() or {}
                threads_raw = data.get('threads', 8)
                gpu_layers_raw = data.get('gpu_layers', 10)
                ctx_size_raw = data.get('ctx_size', 8192)
                
                # Check parameter types to prevent ValueError crash
                def is_valid_int_param(val):
                    if isinstance(val, int) and not isinstance(val, bool):
                        return True
                    if isinstance(val, str):
                        return val.isdigit()
                    return False

                if not (is_valid_int_param(threads_raw) and is_valid_int_param(gpu_layers_raw) and is_valid_int_param(ctx_size_raw)):
                    try:
                        model_switch_lock.release()
                    except RuntimeError:
                        pass
                    return jsonify({'error': '參數必須為正整數'}), 400
                
                threads = int(threads_raw)
                gpu_layers = int(gpu_layers_raw)
                ctx_size = int(ctx_size_raw)
                
                if threads < 1 or threads > 64:
                    threads = 8
                if gpu_layers < 0 or gpu_layers > 256:
                    gpu_layers = 10
                if ctx_size < 512 or ctx_size > 65536:
                    ctx_size = 8192
                    
                config_data = {
                    'threads': threads,
                    'gpu_layers': gpu_layers,
                    'ctx_size': ctx_size
                }
                
                old_config = {'threads': 8, 'gpu_layers': 10, 'ctx_size': 8192}
                if os.path.exists(config_path):
                    try:
                        with open(config_path, 'r') as cf:
                            old_config = json.load(cf)
                    except Exception:
                        pass

                with open(config_path, 'w') as f:
                    json.dump(config_data, f)
                    
                selected_model_path = os.path.join(config_dir, "selected_model")
                restarted = False
                if os.path.exists(selected_model_path):
                    with open(selected_model_path, 'r') as sf:
                        selected_path = sf.read().strip()
                    if os.path.exists(selected_path):
                        user_systemd = os.path.expanduser("~/.config/systemd/user")
                        service_path = os.path.join(user_systemd, "linebot-llama.service")
                        
                        # Start background thread to apply config and restart llama
                        t = threading.Thread(
                            target=_config_apply_worker,
                            args=(config_data, old_config, config_path, selected_path, service_path, selected_model_path)
                        )
                        t.start()
                        restarted = True
                
                if not restarted:
                    # If not restarted, we should release the lock immediately
                    try:
                        model_switch_lock.release()
                    except RuntimeError:
                        pass
                    return jsonify({'ok': True, 'restarted': False, 'msg': '微調配置已成功儲存！(目前無啟用中的模型)'})

                return jsonify({'ok': True, 'restarted': True, 'msg': '微調配置已儲存，正在背景套用並重啟服務，請稍候。'})
            except Exception as e:
                logger.error(f"Save engine config failed: {e}")
                try:
                    model_switch_lock.release()
                except RuntimeError:
                    pass
                return jsonify({'error': f'儲存微調配置失敗: {str(e)}'}), 500


    @app.route('/api/models/delete', methods=['POST'])
    @login_required
    def delete_model():
        try:
            data = request.get_json() or {}
            model_name = data.get('model_name', '').strip()
            if not model_name:
                return jsonify({'error': '未提供模型名稱'}), 400
                
            if '/' in model_name or '\\' in model_name or '..' in model_name:
                return jsonify({'error': '非法檔案名稱'}), 400
                
            if not model_name.endswith('.gguf'):
                return jsonify({'error': '只能刪除 .gguf 模型檔案'}), 400

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