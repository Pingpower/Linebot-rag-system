import os
import json
import logging
from flask import Flask, request, abort, send_from_directory
from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client, Client
from cachetools import TTLCache
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
    ImageMessage,
    VideoMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# ============================================
# 初始化
# ============================================
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Supabase 連線（使用 service_role key 才能繞過 RLS）
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase 連線已初始化")
else:
    logger.warning("未設定 SUPABASE_URL 或 SUPABASE_SERVICE_KEY，多租戶功能將停用")

# 本地 LLM 客戶端
llm_client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="sk-no-key-required"
)

# 公司設定快取（TTL = 5 分鐘，避免每次請求都查 DB）
company_cache: TTLCache = TTLCache(maxsize=100, ttl=300)

# ============================================
# Supabase Helper Functions
# ============================================

def get_company(slug: str) -> dict | None:
    """從 Supabase 取得公司設定（含快取）"""
    if slug in company_cache:
        return company_cache[slug]

    if not supabase:
        return None

    try:
        result = (
            supabase.table('companies')
            .select('*')
            .eq('slug', slug)
            .eq('is_active', True)
            .single()
            .execute()
        )
        if result.data:
            company_cache[slug] = result.data
            return result.data
    except Exception as e:
        logger.error({"msg": "get_company failed", "slug": slug, "error": str(e)})
    return None


def search_knowledge(company_id: str, query: str, limit: int = 3) -> list[dict]:
    """全文搜尋公司知識庫（以 Python 本地字元/N-Gram 評分做中文模糊檢索，解決 Postgres Simple FTS 斷詞問題）"""
    if not supabase:
        return []
    try:
        # 1. 撈取該公司所有啟用的知識條目
        res = (
            supabase.table('knowledge_base')
            .select('title, content')
            .eq('company_id', company_id)
            .eq('is_active', True)
            .execute()
        )
        all_docs = res.data or []
        if not all_docs:
            return []
            
        # 2. 清理查詢語句並做中文字元分詞
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
                
        # 4. 排序並回傳
        scored_docs.sort(key=lambda x: x[0], reverse=True)
        return [doc for score, doc in scored_docs[:limit]]
        
    except Exception as e:
        logger.warning({"msg": "knowledge search failed", "error": str(e)})
        return []


def get_history(company_id: str, user_id: str, rounds: int = 5) -> list[dict]:
    """取得用戶最近 N 輪對話（最舊的在前，符合 LLM messages 格式）"""
    if not supabase:
        return []
    try:
        result = (
            supabase.table('chat_history')
            .select('role, content')
            .eq('company_id', company_id)
            .eq('user_id', user_id)
            .order('created_at', desc=True)
            .limit(rounds * 2)
            .execute()
        )
        return list(reversed(result.data or []))
    except Exception as e:
        logger.warning({"msg": "get_history failed", "error": str(e)})
        return []


def save_messages(company_id: str, user_id: str, user_msg: str, ai_msg: str):
    """一次儲存一對對話記錄"""
    if not supabase:
        return
    try:
        supabase.table('chat_history').insert([
            {'company_id': company_id, 'user_id': user_id, 'role': 'user',      'content': user_msg},
            {'company_id': company_id, 'user_id': user_id, 'role': 'assistant', 'content': ai_msg},
        ]).execute()
    except Exception as e:
        logger.error({"msg": "save_messages failed", "error": str(e)})

def reply_with_flex_or_text(api_client, reply_token: str, company_name: str, ai_reply: str, logo_url: str = None):
    """回覆 LINE 訊息，優先使用精美設計的 Flex Message，支援 [FLEX_CARD] 解析與自訂 Logo"""
    if not ai_reply or not ai_reply.strip():
        ai_reply = "抱歉，我目前無法回答這個問題。"
        
    try:
        messages = []
        has_card = False
        card_data = None
        main_text = ai_reply
        
        start_tag = "[FLEX_CARD]"
        end_tag = "[/FLEX_CARD]"
        
        start_idx = ai_reply.find(start_tag)
        end_idx = ai_reply.find(end_tag)
        
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            has_card = True
            card_json_str = ai_reply[start_idx + len(start_tag) : end_idx].strip()
            main_text = (ai_reply[:start_idx] + "\n" + ai_reply[end_idx + len(end_tag):]).strip()
            
            # 清理模型可能輸出的 markdown code block 包裝
            if card_json_str.startswith("```"):
                nl_idx = card_json_str.find("\n")
                if nl_idx != -1:
                    card_json_str = card_json_str[nl_idx:].strip()
                if card_json_str.endswith("```"):
                    card_json_str = card_json_str[:-3].strip()
                    
            try:
                card_data = json.loads(card_json_str)
            except Exception as pe:
                logger.error({"msg": "Failed to parse FLEX_CARD JSON", "error": str(pe), "json": card_json_str})
                has_card = False
        
        # 1. 處理普通對話文字
        if main_text and main_text.strip():
            messages.append(TextMessage(text=main_text))
            
        # 2. 建立 Header 樣式（包含 Logo 或預設 AI 機器人）
        header_contents = []
        if logo_url:
            header_contents = [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [
                        {
                            "type": "image",
                            "url": logo_url,
                            "size": "xxs",
                            "aspectMode": "fit",
                            "flex": 1,
                            "align": "center",
                            "gravity": "center"
                        },
                        {
                            "type": "box",
                            "layout": "vertical",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "🤖 AI 客服助理",
                                    "weight": "bold",
                                    "color": "#FFFFFF",
                                    "size": "xxs"
                                },
                                {
                                    "type": "text",
                                    "text": company_name,
                                    "weight": "bold",
                                    "color": "#E0E7FF",
                                    "size": "sm",
                                    "margin": "xs"
                                }
                            ],
                            "flex": 4,
                            "margin": "md"
                        }
                    ]
                }
            ]
        else:
            header_contents = [
                {
                    "type": "text",
                    "text": "🤖 AI 客服助理",
                    "weight": "bold",
                    "color": "#FFFFFF",
                    "size": "sm"
                },
                {
                    "type": "text",
                    "text": company_name,
                    "weight": "bold",
                    "color": "#E0E7FF",
                    "size": "lg",
                    "margin": "xs"
                }
            ]
            
        # 3. 建立 FLEX_CARD 氣泡
        if has_card and card_data:
            image_url = card_data.get('imageUrl')
            title = card_data.get('title', '推薦項目')
            text = card_data.get('text', '')
            buttons = card_data.get('buttons', [])
            
            footer_buttons = []
            for btn in buttons:
                label = btn.get('label', '了解更多')
                action = {}
                if btn.get('uri'):
                    action = {
                        "type": "uri",
                        "label": label,
                        "uri": btn['uri']
                    }
                else:
                    action = {
                        "type": "message",
                        "label": label,
                        "text": btn.get('text', label)
                    }
                footer_buttons.append({
                    "type": "button",
                    "style": "primary",
                    "color": "#4F46E5",
                    "height": "sm",
                    "action": action,
                    "margin": "xs"
                })
            
            bubble = {
                "type": "bubble",
                "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#4F46E5",
                    "contents": header_contents,
                    "paddingAll": "md"
                }
            }
            
            if image_url:
                bubble["hero"] = {
                    "type": "image",
                    "url": image_url,
                    "size": "full",
                    "aspectRatio": "20:13",
                    "aspectMode": "cover"
                }
                
            bubble["body"] = {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": title,
                        "weight": "bold",
                        "size": "md",
                        "color": "#1F2937"
                    }
                ],
                "paddingAll": "lg"
            }
            if text:
                bubble["body"]["contents"].append({
                    "type": "text",
                    "text": text,
                    "wrap": True,
                    "size": "sm",
                    "color": "#4B5563",
                    "margin": "sm"
                })
                
            bubble["footer"] = {
                "type": "box",
                "layout": "vertical",
                "spacing": "xs",
                "contents": footer_buttons if footer_buttons else [
                    {
                        "type": "text",
                        "text": "💡 提示：點擊按鈕獲取更多服務",
                        "size": "xxs",
                        "color": "#9CA3AF",
                        "align": "center"
                    }
                ],
                "paddingAll": "md"
            }
            
            flex_container = FlexContainer.from_json(json.dumps(bubble))
            messages.append(FlexMessage(alt_text=f"推薦項目：{title}", contents=flex_container))
            
        else:
            # 如果沒有 card 且 messages 長度為空 (沒有 main_text)，才使用原本的預設 Flex Bubble
            if not messages:
                flex_json = {
                  "type": "bubble",
                  "header": {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#4F46E5",
                    "contents": header_contents,
                    "paddingAll": "md"
                  },
                  "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                      {
                        "type": "text",
                        "text": ai_reply,
                        "wrap": True,
                        "size": "md",
                        "color": "#1F2937"
                      }
                    ],
                    "paddingAll": "lg"
                  },
                  "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                      {
                        "type": "separator",
                        "color": "#F3F4F6",
                        "margin": "sm"
                      },
                      {
                        "type": "text",
                        "text": "💡 提示：本訊息由本地 AI 依據知識庫自動整理生成",
                        "size": "xxs",
                        "color": "#9CA3AF",
                        "margin": "md",
                        "align": "center"
                      }
                    ],
                    "paddingAll": "sm"
                  }
                }
                flex_container = FlexContainer.from_json(json.dumps(flex_json))
                alt_text = f"AI 回覆：{ai_reply[:30]}..."
                messages.append(FlexMessage(alt_text=alt_text, contents=flex_container))
        
        # 4. 發送所有回覆
        if messages:
            MessagingApi(api_client).reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=messages[:5]
                )
            )
            logger.info("Advanced LINE reply sent successfully!")
            
    except Exception as ex:
        logger.error({"msg": "Failed to send advanced Flex Message, falling back to TextMessage", "error": str(ex)})
        try:
            MessagingApi(api_client).reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=ai_reply)]
                )
            )
        except Exception as e2:
            logger.error({"msg": "Final fallback reply failed", "error": str(e2)})

# 用戶最後生成的圖片 URL 記錄，便於一鍵轉影片
user_last_image = {}

def reply_text(company: dict, reply_token: str, text: str):
    """簡便的純文字回覆輔助函數"""
    try:
        config = Configuration(access_token=company['line_access_token'])
        with ApiClient(config) as api_client:
            MessagingApi(api_client).reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        logger.error({"msg": "reply_text failed", "error": str(e)})


def handle_text_event(company: dict, user_id: str, reply_token: str, user_message: str):
    """RAG + LLM 推理 + LINE 回覆"""
    
    # ── 攔截 AI 生圖與生片指令 ──
    msg_lower = user_message.lower().strip()
    if msg_lower.startswith("/draw ") or msg_lower.startswith("/畫圖 "):
        prompt = user_message[6:].strip()
        if not prompt:
            reply_text(company, reply_token, "請輸入繪圖提示詞，例如：/draw 一隻戴著墨鏡的貓")
            return
            
        try:
            import sys
            if "/home/pipadmin/文件" not in sys.path:
                sys.path.append("/home/pipadmin/文件")
            from admin.hf_api import generate_image
            
            img_url = generate_image(prompt)
            if img_url:
                user_last_image[user_id] = img_url
                
                config = Configuration(access_token=company['line_access_token'])
                with ApiClient(config) as api_client:
                    MessagingApi(api_client).reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=reply_token,
                            messages=[
                                ImageMessage(original_content_url=img_url, preview_image_url=img_url),
                                TextMessage(text=f"✨ 這是為您生成的圖片！\n指令: {prompt}\n\n💡 提示：輸入「/video」或「/動起來」可以將此圖片轉為短影片喔！")
                            ]
                        )
                    )
                return
            else:
                reply_text(company, reply_token, "抱歉，圖片生成失敗，請稍候重試。")
                return
        except Exception as e:
            logger.error({"msg": "LINE /draw failed", "error": str(e)})
            reply_text(company, reply_token, "抱歉，圖片生成服務目前不可用。")
            return
            
    elif msg_lower == "/video" or msg_lower == "/動起來":
        img_url = user_last_image.get(user_id)
        if not img_url:
            reply_text(company, reply_token, "您最近沒有生成過圖片喔！請先使用「/draw 您的指令」生成一張圖片。")
            return
            
        try:
            import sys
            if "/home/pipadmin/文件" not in sys.path:
                sys.path.append("/home/pipadmin/文件")
            from admin.hf_api import generate_video
            
            video_url = generate_video(img_url)
            if video_url:
                config = Configuration(access_token=company['line_access_token'])
                with ApiClient(config) as api_client:
                    MessagingApi(api_client).reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=reply_token,
                            messages=[
                                VideoMessage(
                                    original_content_url=video_url, 
                                    preview_image_url=img_url,
                                    tracking_id=f"vid_{user_id[:8]}"
                                ),
                                TextMessage(text="🎬 您的動態短影片已生成完成！")
                            ]
                        )
                    )
                return
            else:
                reply_text(company, reply_token, "抱歉，影片生成失敗（可能 Hugging Face 佇列擁擠），請稍候重試。")
                return
        except Exception as e:
            logger.error({"msg": "LINE /video failed", "error": str(e)})
            reply_text(company, reply_token, "抱歉，影片生成服務目前不可用。")
            return

    # ── 正常 RAG + LLM 對話流程 ──
    # 1. 搜尋知識庫
    docs = search_knowledge(company['id'], user_message)

    # 2. 取得對話歷史
    history = get_history(company['id'], user_id)

    # 3. 組合 system prompt 與檢查是否為 Strict RAG 模式
    original_prompt = company.get('system_prompt', '你是一個友善、專業的 AI 客服助理。')
    is_strict_rag = False

    if original_prompt.startswith('[STRICT_RAG]'):
        is_strict_rag = True
        system_prompt = original_prompt[len('[STRICT_RAG]'):].lstrip()
    else:
        system_prompt = original_prompt

    # 3.1 載入自訂圖文資產/圖示以供 AI 動態輸出 FLEX_CARD
    assets = []
    try:
        assets_res = supabase.table('company_assets').select('*').eq('company_id', company['id']).execute()
        assets = assets_res.data or []
    except Exception as e:
        logger.warning({"msg": "Failed to fetch company assets", "error": str(e)})

    # 自行判斷何時套用 Flex Message 的系統提示語
    assets_block = "\n\n【動態選單與互動圖卡 (Flex Message) 指令】\n"
    assets_block += "1. 為了提供用戶更好的視覺體驗，當用戶諮詢的內容適合結構化展示（如：推薦特定項目、列出服務清單、提供預約/導流按鈕、展示多個選項或引導操作）時，你應自行判斷套用互動圖卡！\n"
    assets_block += "2. 當你決定套用互動圖卡時，請在回覆中嵌入 `[FLEX_CARD]...[/FLEX_CARD]` 標記，格式必須是合法的 JSON 物件（嚴禁包含任何其它字元），格式如下：\n"
    assets_block += "```json\n"
    assets_block += "[FLEX_CARD]\n"
    assets_block += "{\n"
    assets_block += '  "imageUrl": "圖片的 HTTPS URL，若無適合的圖片，請設為 null 或是留空，優先選用下方已上傳的資產圖片",\n'
    assets_block += '  "title": "圖卡標題 (15字以內)",\n'
    assets_block += '  "text": "圖卡說明描述 (30字以內)",\n'
    assets_block += '  "buttons": [\n'
    assets_block += '    {"label": "按鈕文字", "text": "點擊後傳送的文字", "uri": "點擊後開啟的網址，不開啟網址則不設定此欄位或設為 null"}\n'
    assets_block += '  ]\n'
    assets_block += "}\n"
    assets_block += "[/FLEX_CARD]\n"
    assets_block += "```\n"

    if assets:
        assets_block += "3. 用戶已上傳以下合法的圖文資產，當詢問相關問題或需要推薦這些服務時，請優先選用並套用以下資產寫法：\n"
        for asset in assets:
            action_desc = ""
            btn_obj = {"label": asset['name']}
            if asset['action_type'] == 'message':
                action_desc = f"點擊動作：發送文字「{asset['action_value']}」"
                btn_obj["text"] = asset['action_value']
            elif asset['action_type'] == 'uri':
                action_desc = f"點擊動作：開啟網址「{asset['action_value']}」"
                btn_obj["uri"] = asset['action_value']
            else:
                action_desc = "點擊動作：無"

            assets_block += f"- 名稱：{asset['name']}\n"
            assets_block += f"  圖片網址：{asset['url']}\n"
            assets_block += f"  說明：{asset['description']}\n"
            assets_block += f"  {action_desc}\n"
            
            # 提供 AI 拼裝好的範例
            example_json = {
                "imageUrl": asset['url'],
                "title": asset['name'],
                "text": asset['description'][:30],
                "buttons": [btn_obj]
            }
            assets_block += f"  推薦圖卡寫法範例：\n  [FLEX_CARD]{json.dumps(example_json, ensure_ascii=False)}[/FLEX_CARD]\n\n"
    else:
        assets_block += "3. 目前尚未上傳自訂資產圖片。如果你需要，可以自由拼裝不帶圖片的純文字互動卡片（將 imageUrl 設為 null，並自訂 1-3 個按鈕引導用戶進行互動詢問或點擊動作）。\n"
        
    assets_block += "注意：你的回覆中可以同時包含一般對話文字，以及 `[FLEX_CARD]...[/FLEX_CARD]`。你可以將它們結合，用戶即可在 LINE 中看得到非常好看的圖片和互動按鈕！\n"
    system_prompt += assets_block

    if is_strict_rag:
        # 在 Strict RAG 模式下，如果 docs 為空（即知識庫沒有匹配到任何資料），直接拒絕回答，不呼叫 LLM 節省資源！
        if not docs:
            ai_reply = "抱歉，在我的知識庫中找不到與此問題相關的資訊。"
            save_messages(company['id'], user_id, user_message, ai_reply)

            # 回覆 LINE
            config = Configuration(access_token=company['line_access_token'])
            with ApiClient(config) as api_client:
                reply_with_flex_or_text(api_client, reply_token, company.get('name', 'AI 客服助理'), ai_reply, logo_url=company.get('logo_url'))
            return

        # 如果有匹配到資料，則在 system_prompt 加上極嚴格的安全限制
        context = "\n\n".join(f"【{d['title']}】\n{d['content']}" for d in docs)
        system_prompt += f"\n\n以下是公司相關資料，請優先參考：\n\n{context}"
        system_prompt += (
            "\n\n【重要安全指令】\n"
            "請「只」根據上面提供的「公司相關資料」回答問題。如果資料中沒有提到答案，或資料不足以完整回答，"
            "請一律直接回答「抱歉，在我的知識庫中找不到與此問題相關的資訊。」。\n"
            "絕對不可使用你本身的通用知識編造任何資訊，不可與用戶閒聊、講笑話或進行任何與資料無關的對話。"
        )
    else:
        # 正常 RAG 模式
        if docs:
            context = "\n\n".join(f"【{d['title']}】\n{d['content']}" for d in docs)
            system_prompt += f"\n\n以下是公司相關資料，請優先參考：\n\n{context}"

    # 4. 組合 messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 5. 呼叫本地 LLM
    try:
        resp = llm_client.chat.completions.create(
            model="local-model",
            messages=messages,
            temperature=0.3, # lower temperature for factual RAG matching
            max_tokens=2048  # Increased to allow thought trace + answer
        )
        ai_reply = resp.choices[0].message.content
        
        # Defensive check: if the main content is empty, try to get reasoning_content
        if not ai_reply or not ai_reply.strip():
            msg_obj = resp.choices[0].message
            reasoning = getattr(msg_obj, 'reasoning_content', None)
            if not reasoning and hasattr(msg_obj, 'model_extra') and msg_obj.model_extra:
                reasoning = msg_obj.model_extra.get('reasoning_content')
            if not reasoning:
                try:
                    reasoning = msg_obj.get('reasoning_content')
                except:
                    pass
            
            if reasoning and reasoning.strip():
                ai_reply = f"[思考過程]\n{reasoning.strip()}"
            else:
                ai_reply = "抱歉，在我的知識庫中找不到與此問題相關的資訊。"
    except Exception as e:
        logger.error({"msg": "LLM request failed", "error": str(e)})
        ai_reply = "抱歉，AI 服務暫時無法使用，請稍後再試。"

    # 6. 儲存對話記錄
    save_messages(company['id'], user_id, user_message, ai_reply)

    # 7. 回覆 LINE
    config = Configuration(access_token=company['line_access_token'])
    with ApiClient(config) as api_client:
        reply_with_flex_or_text(api_client, reply_token, company.get('name', 'AI 客服助理'), ai_reply, logo_url=company.get('logo_url'))
# ============================================
# 路由：多租戶 Webhook 入口（單一路由，乾淨正確）
# ============================================

@app.route("/callback/<company_slug>", methods=['POST'])
def callback(company_slug: str):
    """
    每間公司擁有獨立的 Webhook URL：
      https://lmbot.pingpower.com.tw/callback/{company_slug}
    """
    # 1. 查詢公司設定
    company = get_company(company_slug)
    if not company:
        logger.warning({"msg": "Unknown slug", "slug": company_slug})
        abort(404)

    # 2. 驗證 LINE 簽名（使用該公司的 channel secret）
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    handler = WebhookHandler(company['line_channel_secret'])
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error({"msg": "Invalid LINE signature", "slug": company_slug})
        abort(400)
    except Exception:
        pass  # handler.handle raises if no @handler.add registered — that's OK

    # 3. 手動解析事件（因為 handler 是動態建立的，無法用裝飾器）
    events = json.loads(body).get('events', [])
    for event in events:
        if event.get('type') != 'message':
            continue
        if event.get('message', {}).get('type') != 'text':
            continue

        handle_text_event(
            company=company,
            user_id=event['source']['userId'],
            reply_token=event['replyToken'],
            user_message=event['message']['text']
        )

    return 'OK'


# ============================================
# 健康檢查端點（LINE Developers 的 Verify 按鈕用）
# ============================================

@app.route("/health", methods=['GET'])
def health():
    return {"status": "ok", "supabase": supabase is not None}, 200


# ============================================
# 靜態資源上傳目錄服務（支援 LINE 獲取 Logo & 自訂圖示）
# ============================================
UPLOAD_FOLDER = "/home/pipadmin/文件/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route("/uploads/<path:filename>", methods=['GET'])
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == "__main__":
    logger.info("多租戶 LINE Bot 伺服器啟動：http://0.0.0.0:5000")
    logger.info("Webhook 格式：/callback/{company_slug}")
    app.run(host="0.0.0.0", port=5000, debug=False)
