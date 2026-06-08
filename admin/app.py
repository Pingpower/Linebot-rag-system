import os
import re
import json
import uuid
import hashlib
import logging
import requests as req_lib
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify, Response)
import subprocess
from dotenv import load_dotenv
from supabase import create_client, Client
try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False
try:
    from duckduckgo_search import DDGS
    DDG_OK = True
except ImportError:
    DDG_OK = False

from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    RichMenuRequest, RichMenuSize, RichMenuArea, RichMenuBounds,
    MessageAction, PostbackAction, URIAction
)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../line_bot/.env'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv('ADMIN_SECRET_KEY', 'change-me-in-production-2026')

ADMIN_USER = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASSWORD', 'admin1234')

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

PLAN_LIMITS = {
    'basic':      {'label': 'Basic',      'limit': 1000,  'color': '#6b7280'},
    'pro':        {'label': 'Pro',        'limit': 5000,  'color': '#3b82f6'},
    'enterprise': {'label': 'Enterprise', 'limit': 99999, 'color': '#8b5cf6'},
}

# ── System Metrics Cache & Background Thread ──────────────────────────────────
import threading
import time

SYSTEM_METRICS_CACHE = {
    'model': '無 / 未啟動',
    'ram': '未知',
    'vram': '未知',
    'vram_used': 0,
    'vram_total': 0,
    'vram_percent': 0.0,
    'cpu_percent': 0.0,
    'ram_used_gb': 0.0,
    'ram_total_gb': 0.0,
    'ram_percent': 0.0,
    'gpu_name': 'NVIDIA GPU',
    'status': '離線 (已停止)',
    'status_color': '#ef4444'
}

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

    while True:
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
            global SYSTEM_METRICS_CACHE
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

        time.sleep(3.0)  # Update every 3 seconds

# Start background thread immediately when app is loaded
metrics_thread = threading.Thread(target=_update_system_metrics_worker, daemon=True)
metrics_thread.start()


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if (request.form.get('username') == ADMIN_USER and
                request.form.get('password') == ADMIN_PASS):
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        flash('帳號或密碼錯誤', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_companies():
    return sb.table('companies').select('*').order('created_at', desc=True).execute().data or []


def get_company(company_id):
    r = sb.table('companies').select('*').eq('id', company_id).single().execute()
    return r.data


def is_expired(company):
    if not company.get('expires_at'):
        return False
    exp = datetime.fromisoformat(company['expires_at'].replace('Z', '+00:00'))
    return exp < datetime.now(timezone.utc)


def month_usage(company_id):
    """本月 inbound 訊息數"""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    r = sb.table('usage_logs') \
        .select('id', count='exact') \
        .eq('company_id', company_id) \
        .eq('direction', 'inbound') \
        .gte('created_at', start) \
        .execute()
    return r.count or 0


def unique_users(company_id):
    """不重複用戶數（all time）"""
    r = sb.rpc('count_unique_users', {'cid': company_id}).execute()
    return r.data if isinstance(r.data, int) else 0

def get_server_metrics():
    global SYSTEM_METRICS_CACHE
    return {
        'model': SYSTEM_METRICS_CACHE.get('model', '無 / 未啟動'),
        'ram': SYSTEM_METRICS_CACHE.get('ram', '未知'),
        'vram': SYSTEM_METRICS_CACHE.get('vram', '未知'),
        'status': SYSTEM_METRICS_CACHE.get('status', '離線'),
        'status_color': SYSTEM_METRICS_CACHE.get('status_color', '#ef4444')
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    companies = get_companies()
    now = datetime.now(timezone.utc)
    stats = {
        'total': len(companies),
        'active': sum(1 for c in companies if c['is_active'] and not is_expired(c)),
        'expired': sum(1 for c in companies if is_expired(c)),
        'expiring_soon': sum(1 for c in companies
                             if c.get('expires_at') and not is_expired(c) and
                             (datetime.fromisoformat(c['expires_at'].replace('Z', '+00:00')) - now).days <= 7),
    }

    # 本月總訊息
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    r = sb.table('usage_logs').select('id', count='exact') \
        .eq('direction', 'inbound').gte('created_at', start_of_month).execute()
    stats['total_messages'] = r.count or 0
    
    # 伺服器狀態
    stats['server'] = get_server_metrics()

    # 各公司本月用量
    for c in companies:
        c['month_usage'] = month_usage(c['id'])
        c['is_expired'] = is_expired(c)
        c['plan_info'] = PLAN_LIMITS.get(c.get('plan', 'basic'), PLAN_LIMITS['basic'])

    return render_template('dashboard.html', stats=stats, companies=companies,
                           plan_limits=PLAN_LIMITS)


# ── Companies CRUD ────────────────────────────────────────────────────────────

@app.route('/companies')
@login_required
def companies():
    data = get_companies()
    for c in data:
        c['month_usage'] = month_usage(c['id'])
        c['is_expired'] = is_expired(c)
        c['plan_info'] = PLAN_LIMITS.get(c.get('plan', 'basic'), PLAN_LIMITS['basic'])
    return render_template('companies.html', companies=data, plan_limits=PLAN_LIMITS)


@app.route('/companies/new', methods=['GET', 'POST'])
@login_required
def company_new():
    if request.method == 'POST':
        f = request.form
        expires_at = f.get('expires_at') or None
        if expires_at:
            expires_at = expires_at + 'T23:59:59+08:00'
        plan = f.get('plan', 'basic')

        # Strict RAG Tag management
        prompt = f.get('system_prompt', '你是一個友善的 AI 客服助理。').strip()
        if prompt.startswith('[STRICT_RAG]'):
            prompt = prompt[len('[STRICT_RAG]'):].lstrip()
        if 'strict_rag' in f:
            prompt = '[STRICT_RAG] ' + prompt

        sb.table('companies').insert({
            'slug':                  f['slug'],
            'name':                  f['name'],
            'line_channel_secret':   f['line_channel_secret'],
            'line_access_token':     f['line_access_token'],
            'system_prompt':         prompt,
            'plan':                  plan,
            'max_messages_per_month': PLAN_LIMITS[plan]['limit'],
            'expires_at':            expires_at,
            'is_active':             True,
        }).execute()
        flash(f'公司「{f["name"]}」已建立！', 'success')
        return redirect(url_for('companies'))
    return render_template('company_edit.html', company=None, plan_limits=PLAN_LIMITS)


@app.route('/companies/<company_id>/edit', methods=['GET', 'POST'])
@login_required
def company_edit(company_id):
    company = get_company(company_id)
    if not company:
        flash('找不到公司', 'error')
        return redirect(url_for('companies'))

    UPLOAD_FOLDER = "/home/pipadmin/文件/uploads"

    if request.method == 'POST':
        f = request.form
        expires_at = f.get('expires_at') or None
        if expires_at:
            expires_at = expires_at + 'T23:59:59+08:00'
        plan = f.get('plan', 'basic')

        # Strict RAG Tag management
        prompt = f.get('system_prompt', '').strip()
        if prompt.startswith('[STRICT_RAG]'):
            prompt = prompt[len('[STRICT_RAG]'):].lstrip()
        if 'strict_rag' in f:
            prompt = '[STRICT_RAG] ' + prompt

        # Logo Upload
        logo_url = company.get('logo_url')
        if 'logo' in request.files:
            file = request.files['logo']
            if file and file.filename != '':
                ext = os.path.splitext(file.filename)[1].lower()
                if ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                    unique_filename = f"logo_{company_id}_{uuid.uuid4().hex[:8]}{ext}"
                    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                    file.save(os.path.join(UPLOAD_FOLDER, unique_filename))
                    logo_url = f"https://lmbot.pingpower.com.tw/uploads/{unique_filename}"

        sb.table('companies').update({
            'name':                  f['name'],
            'line_channel_secret':   f['line_channel_secret'],
            'line_access_token':     f['line_access_token'],
            'system_prompt':         prompt,
            'plan':                  plan,
            'max_messages_per_month': PLAN_LIMITS[plan]['limit'],
            'expires_at':            expires_at,
            'is_active':             'is_active' in f,
            'logo_url':              logo_url,
        }).eq('id', company_id).execute()
        flash(f'已更新公司資料', 'success')
        return redirect(url_for('companies'))

    # GET: Clean company system_prompt so [STRICT_RAG] doesn't show in UI
    company_clean = dict(company)
    if company_clean.get('system_prompt', '').startswith('[STRICT_RAG]'):
        company_clean['system_prompt'] = company_clean['system_prompt'][len('[STRICT_RAG]'):].lstrip()

    # 獲取公司的自訂資產
    assets = []
    try:
        assets_res = sb.table('company_assets').select('*').eq('company_id', company_id).order('created_at', desc=True).execute()
        assets = assets_res.data or []
    except Exception as e:
        logger.warning({"msg": "Failed to fetch company assets in admin", "error": str(e)})

    return render_template('company_edit.html', company=company_clean, plan_limits=PLAN_LIMITS, assets=assets)


# ── Company Assets Management ──────────────────────────────────────────────────

@app.route('/companies/<company_id>/assets/add', methods=['POST'])
@login_required
def company_asset_add(company_id):
    UPLOAD_FOLDER = "/home/pipadmin/文件/uploads"
    if 'file' not in request.files:
        flash('沒有上傳檔案', 'error')
        return redirect(url_for('company_edit', company_id=company_id))
    
    file = request.files['file']
    if not file or file.filename == '':
        flash('沒有選擇檔案', 'error')
        return redirect(url_for('company_edit', company_id=company_id))
    
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
        flash('僅支援圖片檔案格式（PNG, JPG, JPEG, GIF, WEBP）', 'error')
        return redirect(url_for('company_edit', company_id=company_id))
    
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    action_type = request.form.get('action_type', 'none')
    action_value = request.form.get('action_value', '').strip()
    
    if not name or not description:
        flash('請填寫資產名稱和用途描述', 'error')
        return redirect(url_for('company_edit', company_id=company_id))
        
    unique_filename = f"asset_{company_id}_{uuid.uuid4().hex[:12]}{ext}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    file.save(os.path.join(UPLOAD_FOLDER, unique_filename))
    url = f"https://lmbot.pingpower.com.tw/uploads/{unique_filename}"
    
    sb.table('company_assets').insert({
        'company_id': company_id,
        'name': name,
        'url': url,
        'description': description,
        'action_type': action_type,
        'action_value': action_value
    }).execute()
    
    flash('成功新增資產與圖卡選單項目，AI 已學會此圖片用途！', 'success')
    return redirect(url_for('company_edit', company_id=company_id))


@app.route('/companies/<company_id>/assets/<asset_id>/delete', methods=['POST'])
@login_required
def company_asset_delete(company_id, asset_id):
    UPLOAD_FOLDER = "/home/pipadmin/文件/uploads"
    # 刪除本地檔案
    try:
        asset_res = sb.table('company_assets').select('*').eq('id', asset_id).execute()
        if asset_res.data:
            asset = asset_res.data[0]
            filename = os.path.basename(asset['url'])
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
    except Exception as e:
        logger.warning({"msg": "Failed to delete asset file from disk", "error": str(e)})

    sb.table('company_assets').delete().eq('id', asset_id).execute()
    flash('已刪除該項資產', 'success')
    return redirect(url_for('company_edit', company_id=company_id))


@app.route('/companies/<company_id>/delete', methods=['POST'])
@login_required
def company_delete(company_id):
    company = get_company(company_id)
    sb.table('companies').delete().eq('id', company_id).execute()
    flash(f'已刪除公司「{company["name"]}」', 'success')
    return redirect(url_for('companies'))


@app.route('/companies/<company_id>/toggle', methods=['POST'])
@login_required
def company_toggle(company_id):
    company = get_company(company_id)
    sb.table('companies').update({'is_active': not company['is_active']}).eq('id', company_id).execute()
    return jsonify({'ok': True, 'is_active': not company['is_active']})


# ── LINE Rich Menu Management ──────────────────────────────────────────────────

@app.route('/rich_menu')
@login_required
def rich_menu():
    companies = get_companies()
    selected_id = request.args.get('company_id')
    
    # Auto redirect to the first company if not specified
    if not selected_id and companies:
        return redirect(url_for('rich_menu', company_id=companies[0]['id']))
        
    selected = None
    menus = []
    if selected_id:
        selected = get_company(selected_id)
        # Fetch rich menus for this company
        r = sb.table('company_rich_menus').select('*').eq('company_id', selected_id).order('created_at', desc=True).execute()
        menus = r.data or []
        
    return render_template('rich_menu.html', companies=companies, selected=selected, menus=menus)


@app.route('/companies/<company_id>/richmenu/upload', methods=['POST'])
@login_required
def rich_menu_upload(company_id):
    company = get_company(company_id)
    if not company:
        return jsonify({'error': 'Company not found'}), 404
        
    token = company.get('line_access_token')
    if not token:
        return jsonify({'error': 'LINE Access Token not configured for this company'}), 400
        
    name = request.form.get('name', '').strip()
    chat_bar_text = request.form.get('chat_bar_text', '').strip()
    width_val = request.form.get('width', type=int)
    height_val = request.form.get('height', type=int)
    areas_json = request.form.get('areas', '[]')
    
    image_file = request.files.get('image')
    if not image_file:
        return jsonify({'error': 'No image file uploaded'}), 400
        
    if not name or not chat_bar_text or not width_val or not height_val:
        return jsonify({'error': 'Missing required fields'}), 400

    if width_val != 1200 or height_val != 810:
        return jsonify({'error': '圖片尺寸不符合規範。本系統強制限制寬度必須為 1200px，高度必須為 810px。'}), 400

    img_bytes = image_file.read()
    if len(img_bytes) > 1 * 1024 * 1024:
        return jsonify({'error': 'Image size exceeds 1MB limit'}), 400
        
    image_file.seek(0)
    
    # Verify physical image dimensions using PIL to prevent bypass
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size
        if w != 1200 or h != 810:
            return jsonify({'error': f'上傳的圖片實際尺寸為 {w}x{h}px，不符合系統要求的 1200x810px 限制。'}), 400
    except Exception as e:
        logger.warning("Failed to validate uploaded image dimensions: %s", str(e))
        return jsonify({'error': '圖片格式有誤或無法解析實際尺寸。'}), 400

    try:
        areas_data = json.loads(areas_json)
    except Exception as e:
        logger.warning("Invalid areas JSON format: %s", str(e))
        return jsonify({'error': f'Invalid areas JSON format: {str(e)}'}), 400

    try:
        # 1. Initialize LINE API client
        config = Configuration(access_token=token)
        api_client = ApiClient(config)
        messaging_api = MessagingApi(api_client)
        messaging_api_blob = MessagingApiBlob(api_client)
        
        # 2. Build RichMenuArea list
        areas_objects = []
        for idx, a in enumerate(areas_data):
            bounds = RichMenuBounds(
                x=int(a['bounds']['x']),
                y=int(a['bounds']['y']),
                width=int(a['bounds']['width']),
                height=int(a['bounds']['height'])
            )
            act_type = a['action']['type']
            label = a['action'].get('label', f'Action {idx+1}')
            if label and len(label) > 20:
                label = label[:20]
                
            if act_type == 'message':
                act = MessageAction(text=a['action']['text'], label=label)
            elif act_type == 'postback':
                act = PostbackAction(
                    data=a['action']['data'],
                    text=a['action'].get('text'),
                    label=label
                )
            elif act_type == 'uri':
                act = URIAction(uri=a['action']['uri'], label=label)
            else:
                return jsonify({'error': f'Unsupported action type: {act_type}'}), 400
                
            areas_objects.append(RichMenuArea(bounds=bounds, action=act))
            
        rich_menu_request = RichMenuRequest(
            size=RichMenuSize(width=width_val, height=height_val),
            selected=False,
            name=name,
            chat_bar_text=chat_bar_text,
            areas=areas_objects
        )
        
        # 3. Create Rich Menu on LINE
        res = messaging_api.create_rich_menu(rich_menu_request)
        rich_menu_id = res.rich_menu_id
        
        # 4. Upload Image to LINE
        messaging_api_blob.set_rich_menu_image(
            rich_menu_id=rich_menu_id,
            body=img_bytes,
            headers={'Content-Type': image_file.content_type}
        )
        
        # 5. Save image locally for dashboard view
        upload_dir = os.path.join(app.root_path, 'static', 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        
        ext = 'png'
        if 'jpeg' in image_file.content_type or 'jpg' in image_file.content_type:
            ext = 'jpg'
        local_filename = f"rich_menu_{rich_menu_id}.{ext}"
        local_filepath = os.path.join(upload_dir, local_filename)
        
        with open(local_filepath, 'wb') as f:
            f.write(img_bytes)
            
        image_url = f"/static/uploads/{local_filename}"
        
        # 6. Record to database
        sb.table('company_rich_menus').insert({
            'company_id': company_id,
            'rich_menu_id': rich_menu_id,
            'name': name,
            'chat_bar_text': chat_bar_text,
            'image_url': image_url,
            'areas': areas_data,
            'is_active': False
        }).execute()
        
        logger.info("Created Rich Menu %s for company %s", rich_menu_id, company_id)
        return jsonify({'success': True, 'rich_menu_id': rich_menu_id})
        
    except Exception as e:
        logger.exception("Failed to create rich menu")
        return jsonify({'error': f"LINE API error: {str(e)}"}), 500


@app.route('/companies/<company_id>/richmenu/<rich_menu_id>/activate', methods=['POST'])
@login_required
def rich_menu_activate(company_id, rich_menu_id):
    company = get_company(company_id)
    if not company:
        return jsonify({'error': 'Company not found'}), 404
        
    token = company.get('line_access_token')
    if not token:
        return jsonify({'error': 'LINE Access Token not configured'}), 400
        
    try:
        config = Configuration(access_token=token)
        api_client = ApiClient(config)
        messaging_api = MessagingApi(api_client)
        
        # Set default rich menu for LINE official account
        messaging_api.set_default_rich_menu(rich_menu_id)
        
        # Update is_active statuses in DB
        sb.table('company_rich_menus').update({'is_active': False}).eq('company_id', company_id).execute()
        sb.table('company_rich_menus').update({'is_active': True}).eq('rich_menu_id', rich_menu_id).execute()
        
        logger.info("Activated Rich Menu %s as default for company %s", rich_menu_id, company_id)
        return jsonify({'success': True})
    except Exception as e:
        logger.exception("Failed to activate rich menu")
        return jsonify({'error': f"LINE API error: {str(e)}"}), 500


@app.route('/companies/<company_id>/richmenu/deactivate', methods=['POST'])
@login_required
def rich_menu_deactivate(company_id):
    company = get_company(company_id)
    if not company:
        return jsonify({'error': 'Company not found'}), 404
        
    token = company.get('line_access_token')
    if not token:
        return jsonify({'error': 'LINE Access Token not configured'}), 400
        
    try:
        config = Configuration(access_token=token)
        api_client = ApiClient(config)
        messaging_api = MessagingApi(api_client)
        
        # Cancel default rich menu
        messaging_api.cancel_default_rich_menu()
        
        # Update DB status
        sb.table('company_rich_menus').update({'is_active': False}).eq('company_id', company_id).execute()
        
        logger.info("Deactivated default Rich Menu for company %s", company_id)
        return jsonify({'success': True})
    except Exception as e:
        logger.exception("Failed to deactivate rich menu")
        return jsonify({'error': f"LINE API error: {str(e)}"}), 500


@app.route('/companies/<company_id>/richmenu/<rich_menu_id>/delete', methods=['POST'])
@login_required
def rich_menu_delete(company_id, rich_menu_id):
    company = get_company(company_id)
    if not company:
        return jsonify({'error': 'Company not found'}), 404
        
    token = company.get('line_access_token')
    if not token:
        return jsonify({'error': 'LINE Access Token not configured'}), 400
        
    try:
        config = Configuration(access_token=token)
        api_client = ApiClient(config)
        messaging_api = MessagingApi(api_client)
        
        # Delete from LINE
        try:
            messaging_api.delete_rich_menu(rich_menu_id)
        except Exception as api_err:
            logger.warning("Failed to delete Rich Menu %s from LINE: %s", rich_menu_id, str(api_err))
            
        # Clean local cache file
        r = sb.table('company_rich_menus').select('image_url').eq('rich_menu_id', rich_menu_id).single().execute()
        if r.data and r.data.get('image_url'):
            local_filename = os.path.basename(r.data['image_url'])
            local_filepath = os.path.join(app.root_path, 'static', 'uploads', local_filename)
            if os.path.exists(local_filepath):
                try:
                    os.remove(local_filepath)
                except Exception as file_err:
                    logger.warning("Failed to remove local file %s: %s", local_filepath, str(file_err))
                    
        # Delete from database
        sb.table('company_rich_menus').delete().eq('rich_menu_id', rich_menu_id).execute()
        
        logger.info("Deleted Rich Menu %s for company %s", rich_menu_id, company_id)
        return jsonify({'success': True})
    except Exception as e:
        logger.exception("Failed to delete rich menu")
        return jsonify({'error': f"Failed to delete: {str(e)}"}), 500


# ── Chat History (對話紀錄) ──────────────────────────────────────────────────

@app.route('/history')
@login_required
def chat_history_view():
    companies = get_companies()
    selected_id = request.args.get('company_id')
    
    # 防呆：若未選定公司且存在公司列表，自動選擇第一家並重定向
    if not selected_id and companies:
        return redirect(url_for('chat_history_view', company_id=companies[0]['id']))
        
    selected_user = request.args.get('user_id', '').strip()
    
    users_list = []
    messages = []
    selected = None
    
    if selected_id:
        selected = get_company(selected_id)
        
        # 1. 取得該公司有對話的最近用戶清單
        try:
            res = sb.table('chat_history') \
                .select('*') \
                .eq('company_id', selected_id) \
                .order('created_at', desc=True) \
                .limit(200) \
                .execute()
            
            all_logs = res.data or []
            
            # 用戶分組邏輯 (保留最新的一筆作預覽)
            seen_users = {}
            for log in all_logs:
                uid = log.get('user_id')
                if not uid:
                    continue
                if uid not in seen_users:
                    seen_users[uid] = {
                        'user_id': uid,
                        'last_message': log.get('content', '')[:30],
                        'last_time': log.get('created_at'),
                        'role': log.get('role')
                    }
            users_list = list(seen_users.values())
        except Exception as e:
            logger.error(f"Failed to fetch chat history users: {e}")
            
        # 2. 取得選定用戶的對話詳情
        if selected_user:
            try:
                res_msgs = sb.table('chat_history') \
                    .select('*') \
                    .eq('company_id', selected_id) \
                    .eq('user_id', selected_user) \
                    .order('created_at', desc=False) \
                    .execute()
                messages = res_msgs.data or []
            except Exception as e:
                logger.error(f"Failed to fetch chat messages for user {selected_user}: {e}")
                
    return render_template('history.html', companies=companies, selected=selected,
                           users_list=users_list, messages=messages, selected_user=selected_user)


@app.route('/knowledge/add_from_history', methods=['POST'])
@login_required
def knowledge_add_from_history():
    try:
        data = request.get_json() or {}
        company_id = data.get('company_id')
        title = data.get('title', '').strip()
        content = data.get('content', '').strip()
        tags_str = data.get('tags', '').strip()
        
        if not company_id or not title or not content:
            return jsonify({'error': '缺少必要欄位'}), 400
            
        tags = [t.strip() for t in tags_str.split(',') if t.strip()] if tags_str else []
        
        sb.table('knowledge_base').insert({
            'company_id': company_id,
            'title': title,
            'content': content,
            'tags': tags,
            'is_active': True
        }).execute()
        
        return jsonify({'ok': True, 'msg': '已成功將對話轉為知識庫條目！'})
    except Exception as e:
        logger.error(f"Failed to add knowledge from history: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/knowledge/export')
@login_required
def knowledge_export():
    try:
        company_id = request.args.get('company_id')
        if not company_id:
            return "缺少公司 ID", 400
            
        comp_res = sb.table('companies').select('name').eq('id', company_id).single().execute()
        comp_name = comp_res.data.get('name') if comp_res.data else "export"
        
        # 撈取所有知識條目
        res = sb.table('knowledge_base').select('*').eq('company_id', company_id).execute()
        data = res.data or []
        
        clean_data = []
        for d in data:
            clean_data.append({
                'title': d.get('title'),
                'content': d.get('content'),
                'tags': d.get('tags', [])
            })
            
        json_str = json.dumps(clean_data, ensure_ascii=False, indent=2)
        
        return Response(
            json_str,
            mimetype="application/json",
            headers={"Content-disposition": f"attachment; filename=knowledge_base_{comp_name}.json"}
        )
    except Exception as e:
        logger.error(f"Export knowledge failed: {e}")
        return f"匯出失敗: {str(e)}", 500


@app.route('/knowledge/import', methods=['POST'])
@login_required
def knowledge_import():
    try:
        company_id = request.form.get('company_id')
        if not company_id:
            flash("缺少公司 ID", "error")
            return redirect(url_for('knowledge'))
            
        if 'file' not in request.files:
            flash("請選擇檔案上傳", "error")
            return redirect(url_for('knowledge', company_id=company_id))
            
        file = request.files['file']
        if not file or file.filename == '':
            flash("未選擇任何檔案", "error")
            return redirect(url_for('knowledge', company_id=company_id))
            
        filename = file.filename.lower()
        imported_count = 0
        
        # 1. JSON Import
        if filename.endswith('.json'):
            try:
                raw_data = file.read().decode('utf-8', errors='ignore')
                items = json.loads(raw_data)
                if not isinstance(items, list):
                    flash("JSON 檔案格式必須為物件陣列", "error")
                    return redirect(url_for('knowledge', company_id=company_id))
                    
                insert_batch = []
                for item in items:
                    title = item.get('title', '').strip()
                    content = item.get('content', '').strip()
                    tags = item.get('tags', [])
                    if not isinstance(tags, list):
                        tags = [t.strip() for t in str(tags).split(',') if t.strip()]
                    if title and content:
                        insert_batch.append({
                            'company_id': company_id,
                            'title': title,
                            'content': content,
                            'tags': tags,
                            'is_active': True
                        })
                if insert_batch:
                    sb.table('knowledge_base').insert(insert_batch).execute()
                    imported_count = len(insert_batch)
            except Exception as je:
                flash(f"JSON 解析失敗: {str(je)}", "error")
                return redirect(url_for('knowledge', company_id=company_id))
                
        # 2. CSV Import
        elif filename.endswith('.csv'):
            try:
                import csv
                import io
                raw_data = file.read().decode('utf-8', errors='ignore')
                f_stream = io.StringIO(raw_data)
                reader = csv.DictReader(f_stream)
                
                insert_batch = []
                for row in reader:
                    title = (row.get('title') or row.get('標題') or row.get('Question') or '').strip()
                    content = (row.get('content') or row.get('內容') or row.get('Answer') or '').strip()
                    tags_str = (row.get('tags') or row.get('標籤') or '').strip()
                    
                    tags = [t.strip() for t in tags_str.split(',') if t.strip()] if tags_str else []
                    if title and content:
                        insert_batch.append({
                            'company_id': company_id,
                            'title': title,
                            'content': content,
                            'tags': tags,
                            'is_active': True
                        })
                if insert_batch:
                    sb.table('knowledge_base').insert(insert_batch).execute()
                    imported_count = len(insert_batch)
            except Exception as ce:
                flash(f"CSV 解析或讀取失敗: {str(ce)}", "error")
                return redirect(url_for('knowledge', company_id=company_id))
                
        else:
            flash("不支援的檔案格式，請上傳 .json 或 .csv 檔案", "error")
            return redirect(url_for('knowledge', company_id=company_id))
            
        flash(f"成功批次匯入 {imported_count} 筆知識條目！", "success")
        return redirect(url_for('knowledge', company_id=company_id))
    except Exception as e:
        logger.error(f"Import knowledge failed: {e}")
        flash(f"匯入失敗: {str(e)}", "error")
        return redirect(url_for('knowledge'))


# ── Knowledge Base ─────────────────────────────────────────────────────────────

@app.route('/knowledge')
@login_required
def knowledge():
    import math
    companies = get_companies()
    selected_id = request.args.get('company_id')
    
    # 防呆：若未選定公司且存在公司列表，自動選擇第一家並重定向
    if not selected_id and companies:
        return redirect(url_for('knowledge', company_id=companies[0]['id']))
        
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1
    limit = 10
    start = (page - 1) * limit
    end = start + limit - 1
    
    entries = []
    selected = None
    total_count = 0
    total_pages = 1
    page_range = []
    
    if selected_id:
        selected = get_company(selected_id)
        query = sb.table('knowledge_base').select('*', count='exact').eq('company_id', selected_id)
        if q:
            query = query.or_(f"title.ilike.%{q}%,content.ilike.%{q}%")
            
        res = query.order('created_at', desc=True).range(start, end).execute()
        entries = res.data or []
        total_count = res.count or 0
        total_pages = math.ceil(total_count / limit) if total_count > 0 else 1
        
        # 計算要顯示的頁碼範圍 (前後各 2 頁)
        start_page = max(1, page - 2)
        end_page = min(total_pages, page + 2)
        page_range = list(range(start_page, end_page + 1))
        
    return render_template('knowledge.html', companies=companies,
                           selected=selected, entries=entries,
                           q=q, page=page, total_pages=total_pages, 
                           total_count=total_count, page_range=page_range)


@app.route('/knowledge/add', methods=['POST'])
@login_required
def knowledge_add():
    f = request.form
    tags = [t.strip() for t in f.get('tags', '').split(',') if t.strip()]
    sb.table('knowledge_base').insert({
        'company_id': f['company_id'],
        'title':      f['title'],
        'content':    f['content'],
        'tags':       tags,
        'is_active':  True,
    }).execute()
    flash('已新增知識條目', 'success')
    return redirect(url_for('knowledge', company_id=f['company_id']))


@app.route('/knowledge/<entry_id>/delete', methods=['POST'])
@login_required
def knowledge_delete(entry_id):
    entry = sb.table('knowledge_base').select('company_id').eq('id', entry_id).single().execute().data
    sb.table('knowledge_base').delete().eq('id', entry_id).execute()
    flash('已刪除條目', 'success')
    return redirect(url_for('knowledge', company_id=entry['company_id']))


@app.route('/knowledge/search')
@login_required
def knowledge_search():
    company_id = request.args.get('company_id')
    query = request.args.get('q', '').strip()
    if not company_id or not query:
        return jsonify([])
    try:
        # 1. 撈取該公司所有啟用的知識條目
        res = (
            sb.table('knowledge_base')
            .select('title, content')
            .eq('company_id', company_id)
            .eq('is_active', True)
            .execute()
        )
        all_docs = res.data or []
        if not all_docs:
            return jsonify([])
            
        # 2. 清理查詢語句並做簡易中文字元分詞
        import re
        clean_query = re.sub(r'[^\w\s]', '', query)
        stop_words = {
            "的", "了", "和", "是", "就", "都", "而", "及", "與", "或", "在", "以", "等", "之",
            "嗎", "呢", "啊", "吧", "呀", "啦", "哈", "唷", "呢", "的", "那", "這個", "那個",
            "請問", "有", "沒有", "有哪些", "是什麼", "如何", "怎麼", "請客", "客服", "助理",
            "你可以", "我想", "我要", "如何申請", "申請", "介紹", "說明"
        }
        
        query_chars = [c for c in clean_query if c.strip() and c not in stop_words]
        if not query_chars:
            query_chars = [c for c in query if c.strip()]
            
        # 3. 計算重疊度評分
        scored_docs = []
        for doc in all_docs:
            title = doc.get('title', '') or ''
            content = doc.get('content', '') or ''
            score = 0
            
            # N-grams 雙字匹配（權重最高）
            ngrams = []
            for i in range(len(query_chars) - 1):
                ngrams.append("".join(query_chars[i:i+2]))
                
            for ngram in ngrams:
                if ngram in title:
                    score += 10
                if ngram in content:
                    score += 5
                    
            # 單字元匹配（基礎權重）
            for char in query_chars:
                if char in title:
                    score += 2
                if char in content:
                    score += 1
                    
            # 只有分數大於等於 5 分，才視為相關
            if score >= 5:
                scored_docs.append((score, doc))
                
        # 4. 排序並限制回傳數量為 5 筆
        scored_docs.sort(key=lambda x: x[0], reverse=True)
        return jsonify([doc for score, doc in scored_docs[:5]])
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── AI 資料蒐集 ────────────────────────────────────────────────────────────────

LLAMA_URL = os.getenv('LLAMA_SERVER_URL', 'http://127.0.0.1:8080')

def _fetch_url_text(url: str) -> str:
    """抓取 URL 並回傳純文字，最多 8000 字"""
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; LineBotKB/1.0)'}
    r = req_lib.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, 'lxml')
    # 移除 script/style/nav/footer
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
        tag.decompose()
    text = soup.get_text(separator='\n', strip=True)
    # 合併過多空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text[:8000]


def _search_web(query: str, max_results: int = 5) -> list[dict]:
    """用 DuckDuckGo HTML 頁面進行高可靠度搜尋，並自動解析與解碼跳轉 URL，防止 API 被擋"""
    from urllib.parse import urlparse, parse_qs, unquote
    headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    url = f'https://html.duckduckgo.com/html/?q={query}'
    results = []
    try:
        r = req_lib.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        
        for body in soup.find_all('div', class_='result__body')[:max_results]:
            title_el = body.find('a', class_='result__url')
            snippet_el = body.find('a', class_='result__snippet')
            if title_el and snippet_el:
                href = title_el.get('href', '')
                if 'uddg=' in href:
                    try:
                        parsed = urlparse(href)
                        qs = parse_qs(parsed.query)
                        if 'uddg' in qs:
                            href = qs['uddg'][0]
                    except Exception:
                        pass
                results.append({
                    'title': title_el.get_text(strip=True),
                    'href': href,
                    'body': snippet_el.get_text(strip=True)
                })
    except Exception as e:
        logger.error(f"DuckDuckGo HTML search failed: {e}")
        # Fallback to duckduckgo-search package
        try:
            with DDGS() as ddg:
                raw_results = list(ddg.text(query, max_results=max_results))
                for r in raw_results:
                    results.append({
                        'title': r.get('title', ''),
                        'href': r.get('href', ''),
                        'body': r.get('body', '')
                    })
        except Exception as e2:
            logger.error(f"Fallback DDGS search also failed: {e2}")
    return results


def _fallback_extract(content: str) -> list[dict]:
    """超強容錯的知識萃取解析器：當 json.loads 失敗時，嘗試用正則與多種模式復原資料"""
    entries = []
    
    # 模式一：解析 JSON-like 區塊（尋找 `{...}` 結構）
    blocks = re.findall(r'\{([^{}]+)\}', content)
    for block in blocks:
        try:
            # 尋找 title (支援雙引號、單引號、或無引號，寬鬆匹配直到逗號或大括號)
            title = ""
            title_match = re.search(r'["\'\s]*title["\'\s]*:\s*["\']?(.*?)["\']?(?:,|\s*\}|\s*"content"\s*:)', block, re.DOTALL | re.IGNORECASE)
            if title_match:
                title = title_match.group(1).strip()
            else:
                t_m = re.search(r'title["\']?\s*:\s*["\']?([^"\',}]+)', block, re.IGNORECASE)
                if t_m:
                    title = t_m.group(1).strip()
            
            # 尋找 content
            cnt = ""
            content_match = re.search(r'["\'\s]*content["\'\s]*:\s*["\']?(.*?)["\']?(?:,|\s*\})', block, re.DOTALL | re.IGNORECASE)
            if content_match:
                cnt = content_match.group(1).strip()
            else:
                c_m = re.search(r'content["\']?\s*:\s*["\']?([^"}]+)', block, re.IGNORECASE)
                if c_m:
                    cnt = c_m.group(1).strip()
            
            # 尋找 tags
            tags = []
            tags_match = re.search(r'tags["\']?\s*:\s*\[(.*?)\]', block, re.IGNORECASE)
            if tags_match:
                tags = [t.strip().replace('"', '').replace("'", "") for t in tags_match.group(1).split(',') if t.strip()]
            
            # 清理引號與轉義字元
            title = re.sub(r'^["\']|["\']$', '', title).replace('\\"', '"').replace("\\'", "'").strip()
            cnt = re.sub(r'^["\']|["\']$', '', cnt).replace('\\"', '"').replace("\\'", "'").strip()
            
            if title and cnt:
                entries.append({"title": title[:15], "content": cnt, "tags": tags})
        except Exception as e:
            logger.debug(f"Block parse failed: {e}")
            
    # 模式二：如果沒有解析到 JSON 物件，嘗試解析 Markdown 清單/項目標題
    if not entries:
        items = re.split(r'(?:\d+\.|\*|-|###)\s+', content)
        for item in items:
            if not item.strip():
                continue
            title_m = re.search(r'(?:標題|Title)\s*[:：]\s*(.*?)(?:\n|$)', item, re.IGNORECASE)
            content_m = re.search(r'(?:說明|內容|Content|Detail)\s*[:：]\s*(.*?)(?:\n\s*(?:標籤|Tags)|$)', item, re.DOTALL | re.IGNORECASE)
            tags_m = re.search(r'(?:標籤|Tags)\s*[:：]\s*(.*?)(?:\n|$)', item, re.IGNORECASE)
            
            if title_m and content_m:
                t = title_m.group(1).strip()
                c = content_m.group(1).strip()
                t = re.sub(r'^["\']|["\']$', '', t).strip()
                c = re.sub(r'^["\']|["\']$', '', c).strip()
                
                tags = []
                if tags_m:
                    tags = [tg.strip().replace('"', '').replace("'", "") for tg in re.split(r'[,，、\s]+', tags_m.group(1)) if tg.strip()]
                entries.append({"title": t[:15], "content": c, "tags": tags})
                
    return entries


def _llm_extract(raw_text: str, hint: str = '') -> list[dict]:
    """呼叫配置的 LLM (本地/Gemini/NVIDIA NIM/OpenRouter)，從原始文字萃取知識條目 JSON"""
    prompt = f"""你是知識庫整理助手。請從以下文字中萃取出所有重要且清晰的知識條目（如果是商品、服務或 FAQ 介紹，請將每款商品或每個問答獨立建立一條條目，總數在 2-8 個之間）。
{f'重點提示：{hint}' if hint else ''}

【原始文字】
{raw_text[:16000]}

【極重要指令】
請直接且僅回傳 JSON 陣列，直接以 [ 開頭並以 ] 結尾。格式如下：
[
  {{"title": "條目標題（10字以內）", "content": "詳細說明內容（100-300字）", "tags": ["標籤1", "標籤2"]}},
  ...
]

【注意事項】
1. 嚴禁在 JSON 的字串值內部使用未逸出的雙引號。若字串值內有雙引號，必須寫成 \\\"。
2. 請直接輸出 JSON 陣列，不要加入任何解釋文字或 Markdown 外殼包裝。"""

    provider = os.getenv('KNOWLEDGE_LLM_PROVIDER', 'local').lower().strip()
    
    def do_request(prov):
        headers = {'Content-Type': 'application/json'}
        if prov == 'gemini':
            api_key = os.getenv('GEMINI_API_KEY', '').strip()
            if not api_key:
                raise ValueError("GEMINI_API_KEY is missing")
            model = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            
            payload = {
                'contents': [{'parts': [{'text': prompt}]}],
                'generationConfig': {
                    'temperature': 0.2,
                    'maxOutputTokens': 4000,
                }
            }
        else:
            if prov == 'nvidia':
                api_key = os.getenv('NVIDIA_NIM_API_KEY', '').strip()
                if not api_key:
                    raise ValueError("NVIDIA_NIM_API_KEY is missing")
                model = os.getenv('NVIDIA_NIM_MODEL', 'meta/llama-3.1-405b-instruct').strip()
                url = 'https://integrate.api.nvidia.com/v1/chat/completions'
                headers['Authorization'] = f'Bearer {api_key}'
            elif prov == 'openrouter':
                api_key = os.getenv('OPENROUTER_API_KEY', '').strip()
                if not api_key:
                    raise ValueError("OPENROUTER_API_KEY is missing")
                model = os.getenv('OPENROUTER_MODEL', 'google/gemini-2.5-flash').strip()
                url = 'https://openrouter.ai/api/v1/chat/completions'
                headers['Authorization'] = f'Bearer {api_key}'
                headers['HTTP-Referer'] = 'https://github.com/Pingpower/-linebot-rag-system'
                headers['X-Title'] = 'Linebot RAG System'
            else:
                model = 'local'
                url = f'{LLAMA_URL}/v1/chat/completions'

            payload = {
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.2,
                'max_tokens': 4000,
                'stream': False,
            }

        # Retry logic for handling temporary 503/429 errors from LLM APIs
        import time
        max_retries = 4
        retry_delay = 2.0  # initial delay in seconds
        resp = None

        for attempt in range(max_retries):
            try:
                logger.info("Extracting knowledge using LLM Provider: %s (model: %s) [Attempt %d/%d]", 
                            prov, model if prov == 'gemini' else payload.get('model'), attempt + 1, max_retries)
                resp = req_lib.post(url, json=payload, headers=headers, timeout=120)
                resp.raise_for_status()
                break  # Success, exit retry loop
            except Exception as e:
                # Determine if the error is retryable (exclude 400, 401, 403, 404 client errors)
                is_retryable = True
                if hasattr(e, 'response') and e.response is not None:
                    status_code = e.response.status_code
                    if status_code in [400, 401, 403, 404]:
                        is_retryable = False

                if is_retryable and attempt < max_retries - 1:
                    logger.warning("LLM API request failed (attempt %d/%d) with error: %s. Retrying in %.1f seconds...", 
                                   attempt + 1, max_retries, str(e), retry_delay)
                    time.sleep(retry_delay)
                    retry_delay *= 1.5  # exponential backoff
                else:
                    logger.exception("LLM API request failed permanently after %d attempts. Error: %s", attempt + 1, str(e))
                    raise e
        
        if prov == 'gemini':
            raw_content = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        else:
            raw_content = resp.json()['choices'][0]['message']['content'].strip()

        content = raw_content
        content = re.sub(r'<think>[\s\S]*?(</think>|$)', '', content).strip()
        content = re.sub(r'```json\s*', '', content)
        content = re.sub(r'```\s*', '', content)
        content = content.strip()

        start = content.find('[')
        end = content.rfind(']')
        if start != -1 and end != -1 and end > start:
            content = content[start:end+1]
        
        content = re.sub(r',\s*\]', ']', content)
        content = re.sub(r',\s*\}', '}', content)

        def escape_json_newlines(s: str) -> str:
            res_chars = []
            in_str = False
            esc = False
            for char in s:
                if char == '"' and not esc:
                    in_str = not in_str
                if char == '\\' and not esc:
                    esc = True
                else:
                    esc = False
                
                if char == '\n' and in_str:
                    res_chars.append('\\n')
                elif char == '\r' and in_str:
                    res_chars.append('\\r')
                else:
                    res_chars.append(char)
            return "".join(res_chars)

        content = escape_json_newlines(content)

        try:
            return json.loads(content, strict=False)
        except Exception as json_err:
            logger.error("JSON parsing failed for provider %s: %s", prov, str(json_err))
            logger.error("Raw content: %s", raw_content)
            try:
                entries = _fallback_extract(content)
                if entries:
                    logger.info("Fallback parser successfully recovered %d entries", len(entries))
                    return entries
            except Exception as fb_err:
                logger.error("Fallback parser also failed: %s", str(fb_err))
            raise json_err

    try:
        return do_request(provider)
    except Exception as primary_err:
        logger.warning("Primary LLM extraction failed: %s", str(primary_err))
        gemini_key = os.getenv('GEMINI_API_KEY', '').strip()
        if provider != 'gemini' and gemini_key:
            logger.info("Triggering automatic fallback to Gemini...")
            try:
                return do_request('gemini')
            except Exception as fallback_err:
                logger.error("Fallback to Gemini also failed: %s", str(fallback_err))
        
        raise ValueError(f"無法解析 AI 回傳的資料格式：{str(primary_err)}。請縮短文字或提供更簡單的內容後再試。")





@app.route('/knowledge/ai-collect/detect-url', methods=['POST'])
@login_required
def knowledge_detect_url():
    """探測目標網址是否為目錄頁，並抓取子項目清單"""
    try:
        data = request.get_json() or {}
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': '沒有提供網址'}), 400

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        r = req_lib.get(url, headers=headers, timeout=15)
        r.raise_for_status()

        if not BS4_OK:
            return jsonify({'is_index': False, 'error': 'BeautifulSoup class is not available'})

        soup = BeautifulSoup(r.text, 'html.parser')

        # Decompose non-content tags
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript']):
            tag.decompose()

        # Locate candidate content area
        container = soup.find(id='ContentPlaceHolder1_dlindex') or soup.find(id='content_middle') or soup.find('main') or soup

        detected_links = []
        seen_urls = set()

        for a in container.find_all('a', href=True):
            href = a['href'].strip()
            text = a.get_text(strip=True)
            if not text or href.startswith('#') or href.startswith('javascript:'):
                continue

            full_url = urljoin(url, href)
            parsed_full = urlparse(full_url)
            parsed_base = urlparse(url)

            if parsed_full.netloc != parsed_base.netloc:
                continue

            if full_url != url:
                path_lower = parsed_full.path.lower()
                
                # 1. 排除常見靜態資源檔案
                static_exts = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.css', '.js', '.zip', '.rar', '.pdf', '.docx', '.xlsx', '.pptx', '.mp3', '.mp4']
                if any(path_lower.endswith(ext) for ext in static_exts):
                    continue
                    
                # 2. 排除常見管理/導航/無效頁面
                admin_keywords = ['login', 'logout', 'register', 'signin', 'signup', 'sitemap', 'contact', 'about', 'privacy', 'term', 'help']
                if any(k in path_lower for k in admin_keywords):
                    continue
                    
                # 3. 判定是否為內容型子網頁 (聯集條件)
                is_content_link = False
                
                # 條件 A: 原有政府機關特定 aspx 格式
                if any(p in path_lower for p in ['cp.aspx', 'content_list.aspx', 'news_content.aspx', 'active_content.aspx']):
                    is_content_link = True
                # 條件 B: 常見的動態內容或申辦關鍵字
                elif any(p in path_lower for p in ['detail', 'view', 'info', 'faq', 'news', 'apply', 'article', 'post', 'item']):
                    is_content_link = True
                # 條件 C: 含有特定的內容查詢參數
                elif parsed_full.query and any(q in parsed_full.query.lower() for q in ['itemid=', 'id=', 'n=', 'index=', 'cid=', 'pk=', 'post=']):
                    is_content_link = True
                # 條件 D: 通用深度路徑且標題文字字數較長 (防範抓取全域導航選單)
                elif len(text) >= 4 and path_lower.count('/') >= 2:
                    is_content_link = True
                    
                if is_content_link:
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        detected_links.append({
                            'text': text,
                            'url': full_url
                        })

        is_index = len(detected_links) >= 3

        return jsonify({
            'is_index': is_index,
            'links': detected_links
        })
    except Exception as e:
        logger.error(f"Detect URL failed: {e}")
        return jsonify({'error': f'網址探測失敗: {str(e)}'}), 500


@app.route('/knowledge/ai-collect', methods=['POST'])
@login_required
def knowledge_ai_collect():
    """AI 資料蒐集 API — 回傳待確認的知識條目"""
    try:
        # Check if it is a file upload (multipart/form-data)
        if 'file' in request.files:
            file = request.files['file']
            hint = request.form.get('hint', '')
            if not file or file.filename == '':
                return jsonify({'error': '沒有選擇檔案'}), 400

            filename = file.filename
            file_ext = os.path.splitext(filename)[1].lower()

            raw_text = ""
            if file_ext in ('.txt', '.md'):
                raw_text = file.read().decode('utf-8', errors='ignore')
            elif file_ext == '.pdf':
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(file)
                    # Limit to first 10 pages to avoid overloading
                    for page in reader.pages[:10]:
                        text = page.extract_text()
                        if text:
                            raw_text += text + "\n"
                except Exception as ex:
                    return jsonify({'error': f'PDF 解析失敗: {str(ex)}'}), 500
            elif file_ext == '.docx':
                try:
                    from docx import Document
                    doc = Document(file)
                    # Limit to first 100 paragraphs
                    for para in doc.paragraphs[:100]:
                        if para.text:
                            raw_text += para.text + "\n"
                except Exception as ex:
                    return jsonify({'error': f'Word 檔案解析失敗: {str(ex)}'}), 500
            else:
                return jsonify({'error': '不支援的檔案格式，請上傳 .txt, .md, .pdf 或 .docx'}), 400

            if not raw_text.strip():
                return jsonify({'error': '無法從檔案中提取出任何文字內容'}), 400

            entries = _llm_extract(raw_text, hint)
            return jsonify({'ok': True, 'entries': entries, 'source': filename})

        # Otherwise, handle JSON request
        else:
            data = request.get_json()
            if not data:
                return jsonify({'error': '缺少請求內容'}), 400

            mode   = data.get('mode')       # 'url' | 'search' | 'text'
            source = data.get('source', '') # URL / 搜尋關鍵字 / 原始文字
            hint   = data.get('hint', '')   # 使用者補充提示

            if mode == 'url':
                if not BS4_OK:
                    return jsonify({'error': '缺少 beautifulsoup4，請執行 pip install beautifulsoup4'}), 500
                raw = _fetch_url_text(source)
                entries = _llm_extract(raw, hint)
                return jsonify({'ok': True, 'entries': entries, 'source': source})

            elif mode == 'search':
                if not DDG_OK:
                    return jsonify({'error': '缺少 duckduckgo-search，請執行 pip install duckduckgo-search'}), 500
                results = _search_web(source, max_results=3)
                # 把搜尋結果組合成文字
                combined = f"搜尋主題：{source}\n\n"
                for r in results:
                    combined += f"### {r.get('title','')}\n{r.get('body','')}\n\n"
                entries = _llm_extract(combined, hint or source)
                return jsonify({'ok': True, 'entries': entries, 'source': source,
                                'search_results': results})

            elif mode == 'text':
                entries = _llm_extract(source, hint)
                return jsonify({'ok': True, 'entries': entries})

            else:
                return jsonify({'error': '未知模式'}), 400

    except req_lib.exceptions.ConnectionError:
        return jsonify({'error': 'llama-server 尚未啟動或模型還在載入中，請稍後再試'}), 503
    except Exception as e:
        logger.error({'msg': 'ai_collect failed', 'error': str(e)})
        return jsonify({'error': str(e)}), 500


@app.route('/knowledge/ai-save', methods=['POST'])
@login_required
def knowledge_ai_save():
    """批次儲存 AI 生成的知識條目"""
    data = request.get_json()
    company_id = data.get('company_id')
    entries    = data.get('entries', [])  # [{title, content, tags}]

    if not company_id or not entries:
        return jsonify({'error': '缺少必要欄位'}), 400

    saved = 0
    for e in entries:
        tags = e.get('tags', [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',') if t.strip()]
        sb.table('knowledge_base').insert({
            'company_id': company_id,
            'title':      e.get('title', '（未命名）'),
            'content':    e.get('content', ''),
            'tags':       tags,
            'is_active':  True,
        }).execute()
        saved += 1

    return jsonify({'ok': True, 'saved': saved})


# ── Stats ──────────────────────────────────────────────────────────────────────

@app.route('/stats')
@login_required
def stats():
    companies = get_companies()
    selected_id = request.args.get('company_id', companies[0]['id'] if companies else None)
    selected = get_company(selected_id) if selected_id else None
    return render_template('stats.html', companies=companies, selected=selected)


@app.route('/api/stats/<company_id>')
@login_required
def api_stats(company_id):
    """7天每日訊息量 JSON"""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    days = []
    for i in range(6, -1, -1):
        d = now - timedelta(days=i)
        start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = d.replace(hour=23, minute=59, second=59)
        r = sb.table('usage_logs').select('id', count='exact') \
            .eq('company_id', company_id).eq('direction', 'inbound') \
            .gte('created_at', start.isoformat()) \
            .lte('created_at', end.isoformat()).execute()
        days.append({'date': d.strftime('%m/%d'), 'count': r.count or 0})
    return jsonify(days)


# ── Hugging Face & AI Generation ────────────────────────────────────────────────

import threading
import time
from hf_api import generate_image as hf_gen_image, generate_video as hf_gen_video

# 全域模型下載進度追蹤器
DOWNLOAD_STATUS = {}

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



def _get_model_size_billion(model_id):
    import re
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


@app.route('/api/models/search')
@login_required
def api_models_search():
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
        
    # 擴大拉取數量至 100 名以供後端過濾，並使用 full=true 取得更新時間與檔案列表
    url = f"https://huggingface.co/api/models?sort={sort_by}&direction=-1&limit=100&filter=gguf&full=true"
    if query:
        url += f"&search={query}"
        
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
            last_modified_str = m.get('lastModified')
            updated_at = "1970-01-01"
            if last_modified_str:
                try:
                    dt = datetime.strptime(last_modified_str[:10], "%Y-%m-%d")
                    if dt.year < 2024:
                        continue  # 排除 2024 年之前的過時模型
                    updated_at = dt.strftime("%Y-%m-%d")
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
            
            processed_models.append({
                'id': model_id,
                'downloads': downloads,
                'likes': likes,
                'suitable': suitable,
                'updated_at': updated_at,
                'gguf_count': gguf_count
            })
            
        # 4. 直接保留 Hugging Face API 回傳的排序 (依 sort_by 設定之排序)
        # 最多只回傳前 25 個精選模型以防頁面過長
        return jsonify(processed_models[:25])
    except Exception as e:
        logger.error(f"Search Hugging Face models failed: {e}")
        return jsonify({'error': str(e)}), 500


def _is_file_suitable(file_size_bytes, vram_gb, filename=""):
    import re
    file_size_gb = file_size_bytes / (1024 * 1024 * 1024)
    # 過濾小於 10MB 的 dummy 檔案
    if file_size_gb < 0.01:
        return False
        
    # 檢查是否為 MoE 格式，如果是，因為本機有 32GB RAM 可以跑 CPU 混合推理，放寬限制至 26.0 GB
    filename_lower = filename.lower()
    is_moe = "moe" in filename_lower or "8x" in filename_lower or re.search(r'a\d+b', filename_lower) is not None
    
    if is_moe:
        return file_size_gb <= 26.0 # 允許下載並執行 26GB 以下的 MoE 模型
        
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
    global DOWNLOAD_STATUS
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
    global DOWNLOAD_STATUS
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
    global DOWNLOAD_STATUS
    task_id = request.args.get('task_id', '').strip()

    if task_id:
        task = DOWNLOAD_STATUS.get(task_id)
        if task is None:
            return jsonify({'error': '找不到該任務'}), 404
        if task.get('status') == 'downloading':
            del DOWNLOAD_STATUS[task_id]
            logger.info({"msg": "download task cancelled", "task_id": task_id})
            return jsonify({'ok': True, 'msg': '已取消下載任務'})
        del DOWNLOAD_STATUS[task_id]
        logger.info({"msg": "download task cleared", "task_id": task_id})
        return jsonify({'ok': True, 'msg': '已清除任務'})

    # Clear all finished tasks (completed or failed)
    finished = [k for k, v in DOWNLOAD_STATUS.items() if v.get('status') != 'downloading']
    for k in finished:
        del DOWNLOAD_STATUS[k]
    logger.info({"msg": "download tasks cleared", "count": len(finished)})
    return jsonify({'ok': True, 'msg': f'已清除 {len(finished)} 筆已完成/失敗任務'})




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


# ── Model Switch Status & Worker ──────────────────────────────────────────────
SWITCH_STATUS = {
    'status': 'idle',   # 'idle' | 'switching' | 'success' | 'failed'
    'model_name': '',
    'error': None,
    'message': '',
    'last_updated': 0
}

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
        extra_args.append("--flash-attn auto")
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

@app.route('/api/models/switch', methods=['POST'])
@login_required
def api_models_switch():
    global SWITCH_STATUS
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
        if any(kw in m_name_lower for kw in ["gemma-4", "4b", "3b", "2b", "gemma2-2b", "lfm", "a1b"]):
            model_class = "tiny"
        elif any(kw in m_name_lower for kw in ["8b", "7b", "9b", "gemma2-9b"]):
            model_class = "medium"
        elif file_size_gb > 0 and file_size_gb < 6.5:
            model_class = "medium"

        if model_class == "tiny":
            threads = 8
            gpu_layers = 99
            ctx_size = 4096
        elif model_class == "medium":
            threads = 8
            gpu_layers = 18
            ctx_size = 4096
        else:
            threads = 8
            gpu_layers = 10
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
    global SWITCH_STATUS
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


@app.route('/api/system/gpu')
@login_required
def api_system_gpu():
    try:
        global SYSTEM_METRICS_CACHE
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
        env_path = os.path.join(os.path.dirname(__file__), '../line_bot/.env')
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
            
        env_path = os.path.join(os.path.dirname(__file__), '../line_bot/.env')
        
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


@app.route('/api/system/knowledge-config', methods=['GET'])
@login_required
def get_knowledge_config():
    try:
        env_path = os.path.join(os.path.dirname(__file__), '../line_bot/.env')
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
        
        env_path = os.path.join(os.path.dirname(__file__), '../line_bot/.env')
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


if __name__ == '__main__':
    logger.info("管理後台啟動：http://localhost:8888")
    app.run(host='0.0.0.0', port=8888, debug=False)

