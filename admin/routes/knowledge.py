import os
import json
import re
import math
import csv
import io
import requests as req_lib
from flask import render_template, request, redirect, url_for, jsonify, flash, Response
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from config import sb, login_required, get_company, get_companies, logger, PLAN_LIMITS
from services.extractor import _fetch_url_text, _search_web, _llm_extract, DDG_OK

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False

def register_knowledge_routes(app):
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
                    content = file.read().decode('utf-8', errors='ignore')
                    items = json.loads(content)
                    if not isinstance(items, list):
                        flash("JSON 檔案格式有誤（必須為陣列物件）", "error")
                        return redirect(url_for('knowledge', company_id=company_id))
                        
                    insert_batch = []
                    for item in items:
                        title = item.get('title', '').strip()
                        content_val = item.get('content', '').strip()
                        tags = item.get('tags', [])
                        
                        if title and content_val:
                            insert_batch.append({
                                'company_id': company_id,
                                'title': title,
                                'content': content_val,
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
                    content = file.read().decode('utf-8', errors='ignore')
                    f_stream = io.StringIO(content)
                    reader = csv.DictReader(f_stream)
                    
                    insert_batch = []
                    for row in reader:
                        title = (row.get('title') or row.get('標題') or row.get('Question') or '').strip()
                        content_val = (row.get('content') or row.get('內容') or row.get('Answer') or '').strip()
                        tags_str = (row.get('tags') or row.get('標籤') or '').strip()
                        
                        tags = [t.strip() for t in tags_str.split(',') if t.strip()] if tags_str else []
                        if title and content_val:
                            insert_batch.append({
                                'company_id': company_id,
                                'title': title,
                                'content': content_val,
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

    @app.route('/knowledge')
    @login_required
    def knowledge():
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
