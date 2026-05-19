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
    metrics = {
        'model': '無 / 未啟動',
        'ram': '未知',
        'vram': '未知',
        'status': '離線',
        'status_color': '#ef4444'
    }
    try:
        # 1. 檢測 LLaMA 引擎服務狀態
        try:
            cmd_status = "systemctl --user is-active linebot-llama"
            sys_status = subprocess.check_output(cmd_status, shell=True, text=True).strip()
        except subprocess.CalledProcessError as e:
            # systemctl is-active returns non-zero when inactive/failed
            sys_status = e.output.strip() if e.output else "inactive"

        llama_url = os.getenv('LLAMA_SERVER_URL', 'http://127.0.0.1:8080')
        
        if sys_status == "activating":
            metrics['status'] = '啟動中 (加載中)'
            metrics['status_color'] = '#f59e0b'  # warn
        elif sys_status == "active":
            try:
                # 測試 HTTP 連線健康度
                resp = req_lib.get(f"{llama_url}/health", timeout=2)
                if resp.status_code == 200:
                    metrics['status'] = '在線 (正常運作)'
                    metrics['status_color'] = '#22c55e'  # success
                elif resp.status_code == 503 or "Loading model" in resp.text:
                    metrics['status'] = '載入模型中...'
                    metrics['status_color'] = '#f59e0b'  # warn
                else:
                    metrics['status'] = f'異常 (HTTP {resp.status_code})'
                    metrics['status_color'] = '#ef4444'  # danger
            except req_lib.exceptions.ConnectionError:
                # 服務雖然 active，但 port 還沒開，代表正在初始化載入引擎
                metrics['status'] = '啟動中 (載入引擎)...'
                metrics['status_color'] = '#f59e0b'
            except Exception:
                metrics['status'] = '異常'
                metrics['status_color'] = '#ef4444'
        else:
            metrics['status'] = '離線 (已停止)'
            metrics['status_color'] = '#ef4444'

        # 2. 獲取當前運行的模型名稱
        cmd_model = "ps -ef | grep '[l]lama-server' | grep -oP '(?<=--model ).*?(?=\\s|$)' || echo ''"
        out_model = subprocess.check_output(cmd_model, shell=True, text=True).strip()
        if out_model:
            metrics['model'] = out_model.split('/')[-1]
            
        # 3. 獲取系統 RAM 用量
        cmd_ram = "free -h | awk '/^Mem:/ {print $3 \" / \" $2}'"
        metrics['ram'] = subprocess.check_output(cmd_ram, shell=True, text=True).strip()
        
        # 4. 獲取 GPU VRAM 用量
        cmd_vram = "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | awk -F',' '{print $1 \"MB / \" $2 \"MB\"}'"
        out_vram = subprocess.check_output(cmd_vram, shell=True, text=True).strip()
        if out_vram:
            metrics['vram'] = out_vram
    except Exception as e:
        logger.error(f"Error getting server metrics: {e}")
    return metrics


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


# ── Knowledge Base ─────────────────────────────────────────────────────────────

@app.route('/knowledge')
@login_required
def knowledge():
    companies = get_companies()
    selected_id = request.args.get('company_id')
    entries = []
    selected = None
    if selected_id:
        selected = get_company(selected_id)
        entries = sb.table('knowledge_base').select('*') \
            .eq('company_id', selected_id).order('created_at', desc=True).execute().data or []
    return render_template('knowledge.html', companies=companies,
                           selected=selected, entries=entries)


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


def _llm_extract(raw_text: str, hint: str = '') -> list[dict]:
    """呼叫本地 LLM，從原始文字萃取知識條目 JSON"""
    prompt = f"""你是知識庫整理助手。請從以下文字中萃取出 2-3 個清晰的知識條目。
{f'重點提示：{hint}' if hint else ''}

【原始文字】
{raw_text[:2000]}

【極重要指令】
請直接且僅回傳 JSON 陣列，直接以 [ 開頭並以 ] 結尾。格式如下：
[
  {{"title": "條目標題（10字以內）", "content": "詳細說明內容（100-300字）", "tags": ["標籤1", "標籤2"]}},
  ...
]

【注意事項】
1. 嚴禁在 JSON 的字串值內部使用未逸出的雙引號。若字串值內有雙引號，必須寫成 \\\"。
2. 請直接輸出 JSON 陣列，不要加入任何解釋文字或 Markdown 外殼包裝。"""

    payload = {
        'model': 'local',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.2,
        'max_tokens': 1200,
        'stream': False,
    }
    resp = req_lib.post(f'{LLAMA_URL}/v1/chat/completions', json=payload, timeout=300)
    resp.raise_for_status()
    raw_content = resp.json()['choices'][0]['message']['content'].strip()

    content = raw_content
    # 1. 移除 <think>...</think> 區塊 (推理模型會強制輸出)
    content = re.sub(r'<think>[\s\S]*?(</think>|$)', '', content).strip()
    
    # 2. 移除 markdown 語法外殼 (如 ```json ... ```)
    content = re.sub(r'```json\s*', '', content)
    content = re.sub(r'```\s*', '', content)
    content = content.strip()

    # 3. 擷取第一個 [ 到最後一個 ] 之間的內容
    start = content.find('[')
    end = content.rfind(']')
    if start != -1 and end != -1 and end > start:
        content = content[start:end+1]
    
    # 4. 清理結尾多餘逗號
    content = re.sub(r',\s*\]', ']', content)
    content = re.sub(r',\s*\}', '}', content)

    try:
        # 使用 strict=False 允許字串中包含未逸出的控制字元 (如真實換行)
        return json.loads(content, strict=False)
    except Exception as e:
        logger.error("LLM Extraction JSON Parsing Failed!")
        logger.error(f"Error detail: {e}")
        logger.error(f"Raw LLM output:\n{raw_content}")
        logger.error(f"Extracted content to parse:\n{content}")
        
        # fallback 嘗試：如果包含未逸出的雙引號，我們嘗試用正則表達式來抽取出 title & content & tags
        try:
            entries = []
            # 尋找所有像 {"title": "...", "content": "..."} 的區塊
            blocks = re.findall(r'\{\s*"title"\s*:\s*"(.*?)"\s*,\s*"content"\s*:\s*"(.*?)"', content, re.DOTALL)
            for title, cnt in blocks:
                title = title.replace('\\"', '"').replace('"', '').strip()
                cnt = cnt.replace('\\"', '"').replace('"', '').strip()
                # 簡單抓取 tags
                tags_match = re.search(r'"tags"\s*:\s*\[(.*?)\]', content)
                tags = []
                if tags_match:
                    tags = [t.strip().replace('"', '') for t in tags_match.group(1).split(',') if t.strip()]
                entries.append({"title": title, "content": cnt, "tags": tags})
            if entries:
                logger.info(f"Fallback regex-based parser successfully recovered {len(entries)} entries!")
                return entries
        except Exception as e2:
            logger.error(f"Fallback regex parser also failed: {e2}")
            
        raise ValueError(f"無法解析 AI 回傳的資料格式：{e}。請縮短文字或提供更簡單的內容後再試。")


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
                if any(p in path_lower for p in ['cp.aspx', 'content_list.aspx', 'news_content.aspx', 'active_content.aspx']) or (parsed_full.query and 'n=' in parsed_full.query):
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
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        # 進行分塊下載以獲取進度
        response = req_lib.get(download_url, stream=True, timeout=30)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        total_mb = round(total_size / (1024 * 1024), 1)
        DOWNLOAD_STATUS[model_id]["total_mb"] = total_mb
        
        downloaded = 0
        start_time = time.time()
        
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                downloaded_mb = round(downloaded / (1024 * 1024), 1)
                
                # 計算時間、進度與速度
                elapsed = time.time() - start_time
                speed = round(downloaded_mb / elapsed, 2) if elapsed > 0 else 0
                percent = int(100 * downloaded / total_size) if total_size > 0 else 0
                
                DOWNLOAD_STATUS[model_id].update({
                    "percent": percent,
                    "downloaded_mb": downloaded_mb,
                    "speed": f"{speed}MB/s"
                })
        
        DOWNLOAD_STATUS[model_id]["status"] = "completed"
        logger.info(f"Model {model_id} downloaded successfully to {save_path}")
    except Exception as e:
        DOWNLOAD_STATUS[model_id].update({
            "status": "failed",
            "error": str(e)
        })
        logger.error(f"Download model failed for {model_id}: {e}")
        # 清理未完成的殘留檔案
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except:
                pass


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


@app.route('/api/models/search')
@login_required
def api_models_search():
    query = request.args.get('q', '').strip()
    # 擴大拉取數量至 60 名以供後端過濾，並使用 full=true 取得更新時間與檔案列表
    url = "https://huggingface.co/api/models?sort=downloads&direction=-1&limit=60&filter=gguf&full=true"
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
            updated_at = "未知"
            if last_modified_str:
                try:
                    dt = datetime.strptime(last_modified_str[:10], "%Y-%m-%d")
                    if dt.year < 2024:
                        continue  # 排除 2024 年之前的過時模型
                    updated_at = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass
            
            # 2. 排除本機(GTX 1060 6GB/16GB RAM)完全無法負擔的超大模型 (如 >=30B)
            model_id_lower = model_id.lower()
            unsuitable_patterns = ['30b', '70b', '120b', '405b', '103b', '72b', '110b', '180b']
            if any(p in model_id_lower for p in unsuitable_patterns):
                continue
                
            # 計算該 Repository 內的 GGUF 檔案數量，若無 GGUF 檔則過濾
            siblings = m.get('siblings', [])
            gguf_count = sum(1 for s in siblings if s.get('rfilename', '').endswith('.gguf'))
            if gguf_count == 0:
                continue
                
            # 3. 標註適合這台 6GB 顯卡執行的主流尺寸 (0.5B ~ 14B)
            # 6GB VRAM 最合適的為 1.5B/3B, 7B/8B 是極限 (需 Q4 量化且可能需 offload 記憶體)
            suitable = any(x in model_id_lower for x in ['0.5b', '1.5b', '3b', '7b', '8b', '9b', '14b', 'gemma-3-1b'])
            
            processed_models.append({
                'id': model_id,
                'downloads': downloads,
                'likes': likes,
                'suitable': suitable,
                'updated_at': updated_at,
                'gguf_count': gguf_count
            })
            
            # 最多只回傳前 25 個精選模型以防頁面過長
            if len(processed_models) >= 25:
                break
                
        return jsonify(processed_models)
    except Exception as e:
        logger.error(f"Search Hugging Face models failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/models/files')
@login_required
def api_models_files():
    model_id = request.args.get('model_id', '').strip()
    if not model_id:
        return jsonify({'error': '缺少 model_id'}), 400
        
    url = f"https://huggingface.co/api/models/{model_id}/tree/main"
    try:
        resp = req_lib.get(url, timeout=10)
        resp.raise_for_status()
        files = resp.json()
        
        gguf_files = []
        for f in files:
            path = f.get('path', '')
            if path.endswith('.gguf'):
                gguf_files.append({
                    'name': path,
                    'size_formatted': f"{round(f.get('size', 0) / (1024*1024*1024), 2)} GB" if f.get('size') else "未知"
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


@app.route('/api/models/switch', methods=['POST'])
@login_required
def api_models_switch():
    try:
        data = request.get_json() or {}
        model_name = data.get('model_name', '').strip()
        if not model_name or not model_name.endswith('.gguf'):
            return jsonify({'error': '無效的模型名稱'}), 400
            
        model_name = os.path.basename(model_name)
        model_dir = "/home/pipadmin/文件/models"
        selected_path = os.path.join(model_dir, model_name)
        
        if not os.path.exists(selected_path):
            return jsonify({'error': '該模型檔案不存在'}), 400
            
        # 1. 寫入 ~/.config/linebot/selected_model 紀錄檔
        config_dir = os.path.expanduser("~/.config/linebot")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "selected_model"), "w") as f:
            f.write(selected_path)
            
        # 2. 更新 systemd 服務配置 (寫入最新 ExecStart)
        user_systemd = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(user_systemd, exist_ok=True)
        service_path = os.path.join(user_systemd, "linebot-llama.service")
        
        service_content = f"""[Unit]
Description=LINE Bot LLaMA Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/pipadmin/文件
ExecStart=/home/pipadmin/文件/llama.cpp/build/bin/llama-server \\
    --model {selected_path} \\
    --host 127.0.0.1 \\
    --port 8080 \\
    --ctx-size 8192 \\
    --n-gpu-layers 10 \\
    --threads 8 \\
    --parallel 2 \\
    --log-disable
Restart=always
RestartSec=10
StandardOutput=append:/home/pipadmin/文件/llama.log
StandardError=append:/home/pipadmin/文件/llama.log
Environment=HOME=/home/pipadmin

[Install]
WantedBy=default.target
"""
        with open(service_path, "w") as f:
            f.write(service_content)
            
        # 3. 重新載入 Systemd 並重啟服務
        subprocess.run("systemctl --user daemon-reload", shell=True, check=True)
        subprocess.run("systemctl --user stop linebot-llama", shell=True)
        subprocess.run("pkill -9 -f 'llama-server'", shell=True)
        time.sleep(1)
        subprocess.run("systemctl --user start linebot-llama", shell=True, check=True)
        
        return jsonify({'ok': True, 'msg': f'已切換至模型 {model_name}，服務重新啟動中。'})
    except Exception as e:
        logger.error(f"Switch model failed: {e}")
        return jsonify({'error': f'切換模型失敗: {str(e)}'}), 500


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
        # 1. 取得 GPU 名稱
        cmd_name = "nvidia-smi --query-gpu=name --format=csv,noheader"
        gpu_name = subprocess.check_output(cmd_name, shell=True, text=True).strip()
        
        # 2. 取得 VRAM 用量
        cmd_vram = "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits"
        out_vram = subprocess.check_output(cmd_vram, shell=True, text=True).strip()
        
        used, total = 0, 0
        if out_vram:
            parts = [p.strip() for p in out_vram.split(',')]
            if len(parts) >= 2:
                used = int(parts[0])
                total = int(parts[1])
                
        return jsonify({
            'name': gpu_name,
            'vram_used': used,
            'vram_total': total,
            'vram_percent': round((used / total) * 100, 1) if total > 0 else 0
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


if __name__ == '__main__':
    logger.info("管理後台啟動：http://localhost:8888")
    app.run(host='127.0.0.1', port=8888, debug=False)

