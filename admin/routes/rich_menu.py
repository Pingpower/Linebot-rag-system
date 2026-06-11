import os
import json
from flask import render_template, request, redirect, url_for, jsonify, current_app
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    RichMenuRequest, RichMenuSize, RichMenuArea, RichMenuBounds,
    MessageAction, PostbackAction, URIAction
)
from config import sb, login_required, get_company, get_companies, logger

def register_rich_menu_routes(app):
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
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
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
                local_filepath = os.path.join(current_app.root_path, 'static', 'uploads', local_filename)
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
