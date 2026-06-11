import os
import uuid
import subprocess
import requests as req_lib
from flask import request, jsonify

from config import login_required, logger, SYSTEM_METRICS_CACHE
from hf_api import generate_image as hf_gen_image, generate_video as hf_gen_video

def _read_env_vars(env_path):
    vars_dict = {}
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line_str = line.strip()
                if line_str and not line_str.startswith('#') and '=' in line_str:
                    parts = line_str.split('=', 1)
                    key = parts[0].strip()
                    val = parts[1].strip().strip('"').strip("'")
                    vars_dict[key] = val
    return vars_dict


def _mask_api_key(key):
    if not key:
        return ""
    if len(key) > 8:
        return key[:4] + "*" * (len(key) - 8) + key[-4:]
    return "****"


def register_system_routes(app):

    @app.route('/api/system/gpu')
    @login_required
    def api_system_gpu():
        try:
            return jsonify({
                'name': SYSTEM_METRICS_CACHE.get('gpu_name', 'NVIDIA GPU'),
                'vram_used': SYSTEM_METRICS_CACHE.get('vram_used', 0),
                'vram_total': SYSTEM_METRICS_CACHE.get('vram_total', 0),
                'vram_percent': SYSTEM_METRICS_CACHE.get('vram_percent', 0.0),
                'cpu_percent': SYSTEM_METRICS_CACHE.get('cpu_percent', 0.0),
                'ram_used': SYSTEM_METRICS_CACHE.get('ram_used_gb', 0.0),
                'ram_total': SYSTEM_METRICS_CACHE.get('ram_total_gb', 0.0),
                'ram_percent': SYSTEM_METRICS_CACHE.get('ram_percent', 0.0)
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500


    @app.route('/api/system/config', methods=['GET'])
    @login_required
    def get_system_config():
        try:
            env_path = os.path.join(os.path.dirname(__file__), '../../line_bot/.env')
            hf_token = ""
            if os.path.exists(env_path):
                with open(env_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip().startswith('HF_TOKEN='):
                            parts = line.strip().split('=', 1)
                            if len(parts) > 1:
                                hf_token = parts[1].strip().strip('"').strip("'")
            
            masked_token = ""
            if hf_token:
                if len(hf_token) > 8:
                    masked_token = hf_token[:4] + "*" * (len(hf_token) - 8) + hf_token[-4:]
                else:
                    masked_token = "****"
            return jsonify({
                "has_token": bool(hf_token),
                "masked_token": masked_token
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500


    @app.route('/api/system/config', methods=['POST'])
    @login_required
    def save_system_config():
        try:
            data = request.get_json() or {}
            hf_token = data.get('hf_token', '').strip()
            if not hf_token:
                return jsonify({"error": "請提供有效的 token"}), 400
                
            env_path = os.path.join(os.path.dirname(__file__), '../../line_bot/.env')
            
            lines = []
            token_replaced = False
            if os.path.exists(env_path):
                with open(env_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    
            new_lines = []
            for line in lines:
                if line.strip().startswith('HF_TOKEN='):
                    new_lines.append(f'HF_TOKEN="{hf_token}"\n')
                    token_replaced = True
                else:
                    new_lines.append(line)
                    
            if not token_replaced:
                new_lines.append(f'\n# Hugging Face Access Token for image & video generation\nHF_TOKEN="{hf_token}"\n')
                
            with open(env_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            
            # 更新當前執行環境變數，使 admin 服務免重啟立刻生效
            os.environ["HF_TOKEN"] = hf_token
            
            # 重啟 linebot-flask 服務，讓 LINE Bot 也立刻讀取到新的環境變數
            subprocess.run("systemctl --user restart linebot-flask", shell=True, check=True)
            
            return jsonify({"msg": "Hugging Face 憑證已儲存，且 LINE Bot 服務已成功重載！"})
        except Exception as e:
            logger.error(f"Save HF_TOKEN failed: {e}")
            return jsonify({"error": f"儲存失敗: {str(e)}"}), 500


    @app.route('/api/generate/image', methods=['POST'])
    @login_required
    def api_generate_image():
        try:
            data = request.get_json() or {}
            prompt = data.get('prompt', '').strip()
            if not prompt:
                return jsonify({'error': '請提供 Prompt 繪圖指令'}), 400
                
            # 呼叫生圖封裝
            img_bytes = hf_gen_image(prompt)
            
            # 儲存到上傳目錄中
            UPLOAD_FOLDER = "/home/pipadmin/文件/uploads"
            unique_filename = f"gen_{uuid.uuid4().hex[:12]}.png"
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            
            save_path = os.path.join(UPLOAD_FOLDER, unique_filename)
            with open(save_path, 'wb') as f:
                f.write(img_bytes)
                
            url = f"https://lmbot.pingpower.com.tw/uploads/{unique_filename}"
            return jsonify({'ok': True, 'url': url})
        except Exception as e:
            logger.error(f"Image generation failed: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/generate/video', methods=['POST'])
    @login_required
    def api_generate_video():
        try:
            data = request.get_json() or {}
            image_url = data.get('image_url', '').strip()
            if not image_url:
                return jsonify({'error': '請提供來源圖片 URL'}), 400
                
            # 下載圖片二進位 bytes
            img_resp = req_lib.get(image_url, timeout=15)
            img_resp.raise_for_status()
            img_bytes = img_resp.content
            
            # 呼叫生影片封裝
            video_bytes = hf_gen_video(img_bytes)
            
            # 儲存影片
            UPLOAD_FOLDER = "/home/pipadmin/文件/uploads"
            unique_filename = f"video_{uuid.uuid4().hex[:12]}.mp4"
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            
            save_path = os.path.join(UPLOAD_FOLDER, unique_filename)
            with open(save_path, 'wb') as f:
                f.write(video_bytes)
                
            url = f"https://lmbot.pingpower.com.tw/uploads/{unique_filename}"
            return jsonify({'ok': True, 'url': url})
        except Exception as e:
            logger.error(f"Video generation failed: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/system/knowledge-config', methods=['GET'])
    @login_required
    def get_knowledge_config():
        try:
            env_path = os.path.join(os.path.dirname(__file__), '../../line_bot/.env')
            vars_dict = _read_env_vars(env_path)
            
            provider = vars_dict.get('KNOWLEDGE_LLM_PROVIDER', 'local')
            gemini_key = vars_dict.get('GEMINI_API_KEY', '')
            gemini_model = vars_dict.get('GEMINI_MODEL', 'gemini-2.5-flash')
            
            nvidia_key = vars_dict.get('NVIDIA_NIM_API_KEY', '')
            nvidia_model = vars_dict.get('NVIDIA_NIM_MODEL', 'meta/llama-3.1-405b-instruct')
            
            openrouter_key = vars_dict.get('OPENROUTER_API_KEY', '')
            openrouter_model = vars_dict.get('OPENROUTER_MODEL', 'google/gemini-2.5-flash')
            
            return jsonify({
                "provider": provider,
                "gemini_key": _mask_api_key(gemini_key),
                "gemini_model": gemini_model,
                "nvidia_key": _mask_api_key(nvidia_key),
                "nvidia_model": nvidia_model,
                "openrouter_key": _mask_api_key(openrouter_key),
                "openrouter_model": openrouter_model
            })
        except Exception as e:
            logger.error(f"Get knowledge config failed: {e}")
            return jsonify({"error": str(e)}), 500


    @app.route('/api/system/knowledge-config', methods=['POST'])
    @login_required
    def save_knowledge_config():
        try:
            data = request.get_json() or {}
            provider = data.get('provider', 'local').strip().lower()
            
            gemini_key = data.get('gemini_key', '').strip()
            gemini_model = data.get('gemini_model', '').strip()
            
            nvidia_key = data.get('nvidia_key', '').strip()
            nvidia_model = data.get('nvidia_model', '').strip()
            
            openrouter_key = data.get('openrouter_key', '').strip()
            openrouter_model = data.get('openrouter_model', '').strip()
            
            env_path = os.path.join(os.path.dirname(__file__), '../../line_bot/.env')
            current_vars = _read_env_vars(env_path)
            
            # 遮罩防禦性檢查與還原
            if '*' in gemini_key:
                gemini_key = current_vars.get('GEMINI_API_KEY', '')
            if '*' in nvidia_key:
                nvidia_key = current_vars.get('NVIDIA_NIM_API_KEY', '')
            if '*' in openrouter_key:
                openrouter_key = current_vars.get('OPENROUTER_API_KEY', '')
                
            update_dict = {
                'KNOWLEDGE_LLM_PROVIDER': provider,
                'GEMINI_API_KEY': gemini_key,
                'GEMINI_MODEL': gemini_model,
                'NVIDIA_NIM_API_KEY': nvidia_key,
                'NVIDIA_NIM_MODEL': nvidia_model,
                'OPENROUTER_API_KEY': openrouter_key,
                'OPENROUTER_MODEL': openrouter_model
            }
            
            lines = []
            if os.path.exists(env_path):
                with open(env_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    
            replaced = {k: False for k in update_dict.keys()}
            new_lines = []
            
            for line in lines:
                stripped = line.strip()
                matched_key = None
                for key in update_dict.keys():
                    if stripped.startswith(f'{key}='):
                        matched_key = key
                        break
                if matched_key:
                    new_lines.append(f'{matched_key}="{update_dict[matched_key]}"\n')
                    replaced[matched_key] = True
                else:
                    new_lines.append(line)
                    
            # 追加新變數
            for key, val in update_dict.items():
                if not replaced[key]:
                    new_lines.append(f'{key}="{val}"\n')
                    
            with open(env_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
                
            # 同步更新執行環境變數
            for key, val in update_dict.items():
                os.environ[key] = val
                
            # 重啟 LINE Bot 服務
            subprocess.run("systemctl --user restart linebot-flask", shell=True, check=True)
            
            return jsonify({"msg": "知識庫模型配置已更新，且 LINE Bot 服務已成功重啟生效！"})
        except Exception as e:
            logger.error(f"Save knowledge config failed: {e}")
            return jsonify({"error": f"儲存失敗: {str(e)}"}), 500
