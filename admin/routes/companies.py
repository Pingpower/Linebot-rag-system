import os
import uuid
from flask import render_template, request, redirect, url_for, flash, jsonify
from config import sb, login_required, get_company, get_companies, is_expired, month_usage, PLAN_LIMITS, logger

def register_companies_routes(app):
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
