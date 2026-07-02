from flask import render_template, request, redirect, url_for, jsonify
from config import sb, login_required, get_company, get_companies, logger

def register_history_routes(app):
    import os
    import sys
    line_bot_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../line_bot'))
    if line_bot_path not in sys.path:
        sys.path.append(line_bot_path)
    
    try:
        from semantic_cache import get_embedding
    except ImportError:
        get_embedding = None

    def _compute_embedding(title: str, content: str):
        if not get_embedding:
            logger.warning("get_embedding is not available. Embedding set to None.")
            return None
        try:
            text_to_embed = f"標題：{title}\n內容：{content}"
            return get_embedding(text_to_embed)
        except Exception as e:
            logger.error(f"Failed to generate embedding in history module: {e}")
            return None

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
            
            emb = _compute_embedding(title, content)
            sb.table('knowledge_base').insert({
                'company_id': company_id,
                'title': title,
                'content': content,
                'tags': tags,
                'embedding': emb,
                'is_active': True
            }).execute()
            
            return jsonify({'ok': True, 'msg': '已成功將對話轉為知識庫條目！'})
        except Exception as e:
            logger.error(f"Failed to add knowledge from history: {e}")
            return jsonify({'error': f"轉入失敗: {str(e)}"}), 500
