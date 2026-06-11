import os
import logging
import threading
import atexit
from dotenv import load_dotenv
from supabase import create_client, Client
from functools import wraps
from flask import session, redirect, url_for
from datetime import datetime, timezone

# 載入設定
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../line_bot/.env'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

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

# 共享的全域變數與 Event (用於指標監控)
SYSTEM_METRICS_EVENT = threading.Event()
SYSTEM_METRICS_EVENT.set()

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

# ── Auth Decorator ────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Database & Cache Helpers ──────────────────────────────────────────────────
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
