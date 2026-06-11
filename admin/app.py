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

# 導入重構模組與全域共享服務
from config import (
    sb, logger, ADMIN_USER, ADMIN_PASS, PLAN_LIMITS,
    SYSTEM_METRICS_EVENT, SYSTEM_METRICS_CACHE, login_required,
    get_companies, get_company, is_expired, month_usage, unique_users, get_server_metrics
)
from services.metrics import start_metrics_worker
from routes.auth import register_auth_routes
from routes.companies import register_companies_routes
from routes.rich_menu import register_rich_menu_routes
from routes.history import register_history_routes
from routes.knowledge import register_knowledge_routes
from routes.models import register_models_routes
from routes.system import register_system_routes

app = Flask(__name__)
app.secret_key = os.getenv('ADMIN_SECRET_KEY', 'change-me-in-production-2026')

# 啟動系統指標監控背景服務
start_metrics_worker(app)

# 註冊路由模組
register_auth_routes(app)
register_companies_routes(app)
register_rich_menu_routes(app)
register_history_routes(app)
register_knowledge_routes(app)
register_models_routes(app)
register_system_routes(app)

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


# ── Models & System Routes have been moved to submodules ──────────────────────

if __name__ == '__main__':
    logger.info("管理後台啟動：http://localhost:8888")
    app.run(host='0.0.0.0', port=8888, debug=False)

