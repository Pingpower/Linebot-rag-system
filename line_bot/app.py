import os
import re
import json
import logging
import time
import sys
import threading
import ast
from semantic_cache import check_cache, add_to_cache
import urllib.parse
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, Response, JSONResponse
import uvicorn
from dotenv import load_dotenv
from openai import AsyncOpenAI
import openai  # for openai.APITimeoutError, openai.APIConnectionError
from supabase import create_client, Client
from cachetools import TTLCache
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import (
    Configuration,
    AsyncApiClient,
    AsyncMessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
    ImageMessage,
    VideoMessage,
    ShowLoadingAnimationRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# ============================================
# 初始化
# ============================================
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="LM Bot API")

# Supabase 連線（使用 service_role key 才能繞過 RLS）
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase 連線已初始化")
else:
    logger.warning("未設定 SUPABASE_URL 或 SUPABASE_SERVICE_KEY，多租戶功能將停用")

# 本地 LLM 客戶端 (Async)
llm_client = AsyncOpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="sk-no-key-required"
)
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "local-model")

# 公司設定快取（TTL = 5 分鐘，避免每次請求都查 DB）
company_cache: TTLCache = TTLCache(maxsize=100, ttl=300)
company_cache_lock = threading.Lock()

# ============================================
# Supabase Helper Functions
# ============================================

def get_company(slug: str) -> dict | None:
    """從 Supabase 取得公司設定（含快取）"""
    with company_cache_lock:
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
            with company_cache_lock:
                company_cache[slug] = result.data
            return result.data
    except Exception as e:
        logger.error({"msg": "get_company failed", "slug": slug, "error": str(e)})
    return None


# get_embedding is defined in semantic_cache.py — import it to avoid duplication
from semantic_cache import get_embedding


async def search_knowledge(company_id: str, query: str, limit: int = 3) -> list[dict]:
    """使用 Supabase 混合檢索（Vector + FTS + RRF 融合評分）(非同步)"""
    if not supabase:
        logger.warning("Supabase is not initialized. RAG skipped.")
        return []
    try:
        # 1. 生成 query embedding
        query_embedding = await get_embedding(query)
        if not query_embedding:
            logger.warning("Failed to generate query embedding, returning empty search results.")
            return []
            
        # 2. 呼叫 supabase RPC "match_knowledge_hybrid"
        # 參數: query_embedding, query_text, match_count, company_filter
        res = supabase.rpc(
            'match_knowledge_hybrid',
            {
                'query_embedding': query_embedding,
                'query_text': query,
                'match_count': limit,
                'company_filter': company_id
            }
        ).execute()
        
        docs = res.data or []
        logger.info(f"Hybrid RAG search found {len(docs)} documents.")
        return [{'title': doc.get('title', ''), 'content': doc.get('content', '')} for doc in docs]
        
    except Exception as e:
        logger.error({"msg": "knowledge hybrid search failed", "error": str(e)})
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

def _strip_markdown(text: str) -> str:
    """Strip common Markdown formatting symbols so LINE plain-text messages look clean.

    Handles: headings (#), bold/italic (*/_), horizontal rules (---/***),
    inline code (`), fenced code blocks (```), and leading list bullets (*/- ).
    """
    if not text:
        return text

    # Remove fenced code blocks entirely — keep the inner content
    text = re.sub(r'```[^\n]*\n([\s\S]*?)```', lambda m: m.group(1).strip(), text)

    # Remove inline code backticks (keep the text inside)
    text = re.sub(r'`([^`]+)`', r'\1', text)

    # Remove ATX headings (# ## ### etc.) — keep the heading text
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

    # Remove bold/italic markers: **text**, __text__, *text*, _text_
    text = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_\n]+)_{1,3}', r'\1', text)

    # Remove horizontal rules (--- / *** / ___)
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)

    # Normalize list bullets: "* item" or "- item" → "• item"
    text = re.sub(r'^[*\-]\s+', '• ', text, flags=re.MULTILINE)

    # Collapse multiple blank lines into a single blank line
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Fallback defensive strip: strictly remove any remaining markdown signs
    text = text.replace("```json", "").replace("```JSON", "").replace("```", "")
    text = text.replace(",,,json", "").replace(",,,", "")
    text = text.replace("`", "")
    text = text.replace("**", "").replace("__", "")
    text = text.replace("*", "").replace("_", "")

    return text.strip()


async def reply_with_flex_or_text(api_client, reply_token: str, company_name: str, ai_reply: str, logo_url: str = None, user_id: str = None, force_push: bool = False, suggested_buttons: list = None):
    """回覆 LINE 訊息，優先使用精美設計的 Flex Message，支援 [FLEX_CARD] 解析與自訂 Logo，且支援超時自動降級為 Push Message (非同步)"""
    if not ai_reply or not ai_reply.strip():
        ai_reply = "抱歉，我目前無法回答這個問題。"
        
    def escape_newlines_in_quotes(json_str: str) -> str:
        in_quotes = False
        escaped_quotes = False
        result = []
        for char in json_str:
            if char == '"' and not escaped_quotes:
                in_quotes = not in_quotes
                result.append(char)
            elif char == '\\' and in_quotes:
                escaped_quotes = not escaped_quotes
                result.append(char)
            else:
                if char == '\n' and in_quotes:
                    result.append('\\n')
                else:
                    result.append(char)
                escaped_quotes = False
        return "".join(result)

    def _clean_and_parse_json(raw_str: str):
        s = raw_str.strip()
        if "```" in s:
            s = re.sub(r'^```[a-zA-Z0-9]*\s*', '', s)
            s = re.sub(r'\s*```$', '', s)
        s = s.strip()
        start = s.find('{')
        end = s.rfind('}')
        if start != -1 and end != -1 and end > start:
            s = s[start:end+1]
        try:
            return json.loads(s)
        except Exception:
            pass
        try:
            val = ast.literal_eval(s)
            if isinstance(val, (dict, list)):
                return val
        except Exception:
            pass
        try:
            fixed_s = escape_newlines_in_quotes(s)
            return json.loads(fixed_s)
        except Exception:
            pass
        raise ValueError("JSON parsing failed after all fallback attempts.")

    try:
        messages = []
        has_card = False
        card_data = None
        main_text = ""
        
        is_pure_json = False

        # ── 優先嘗試：檢查 ai_reply 是否為一個包含 customer_reply 鍵值的結構化純 JSON ──
        try:
            s_clean = ai_reply.strip()
            if s_clean.startswith("```"):
                s_clean = re.sub(r'^```[a-zA-Z0-9]*\s*', '', s_clean)
                s_clean = re.sub(r'\s*```$', '', s_clean)
                s_clean = s_clean.strip()
            
            start_idx = s_clean.find('{')
            end_idx = s_clean.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_candidate = s_clean[start_idx:end_idx+1]
                parsed = _clean_and_parse_json(json_candidate)
                if isinstance(parsed, dict) and "customer_reply" in parsed:
                    main_text = parsed.get("customer_reply", "").strip()
                    card_data = parsed.get("flex_message")
                    has_card = True if card_data else False
                    main_text = _strip_markdown(main_text)
                    is_pure_json = True
                    logger.info("Successfully parsed LLM reply as a structured pure JSON object.")
        except Exception as e_json:
            logger.debug("Attempt to parse as pure JSON failed, falling back to regex tags: %s", str(e_json))

        # ── 若非純 JSON，退回原本的 regex [FLEX_CARD] 標籤匹配 ──
        if not is_pure_json:
            start_match = re.search(r'\[FLEX[-_\s]?CARD\]', ai_reply, re.IGNORECASE)
            if start_match:
                end_match = re.search(r'\[/FLEX[-_\s]?CARD\]', ai_reply, re.IGNORECASE)
                if end_match and end_match.start() > start_match.end():
                    card_json_str = ai_reply[start_match.end():end_match.start()].strip()
                    main_text = (ai_reply[:start_match.start()] + "\n" + ai_reply[end_match.end():]).strip()
                else:
                    card_json_str = ai_reply[start_match.end():].strip()
                    main_text = ai_reply[:start_match.start()].strip()
                
                try:
                    card_data = _clean_and_parse_json(card_json_str)
                    has_card = True
                    main_text = _strip_markdown(main_text)
                except Exception as pe:
                    logger.error({"msg": "Failed to parse FLEX_CARD JSON after deep cleanup", "error": str(pe), "json": card_json_str})
                    has_card = False
                
        if not has_card:
            # 解析失敗或沒有標籤，抹除所有的 [FLEX_CARD] 以防民眾看見
            main_text = re.sub(r'\[FLEX[-_\s]?CARD\][\s\S]*?\[/FLEX[-_\s]?CARD\]', '', ai_reply, flags=re.IGNORECASE).strip()
            main_text = re.sub(r'\[FLEX[-_\s]?CARD\][\s\S]*', '', main_text, flags=re.IGNORECASE).strip()
            main_text = _strip_markdown(main_text)
        
        # 1. 處理普通對話文字
        if has_card and main_text and main_text.strip():
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
            
        # 3. 建立 FLEX_CARD 氣泡 (支援自訂原生官方 Flex 語法或簡易圖卡格式)
        if has_card and card_data:
            if isinstance(card_data, dict) and card_data.get('type') in ['bubble', 'carousel']:
                try:
                    flex_container = FlexContainer.from_json(json.dumps(card_data))
                    alt_title = "推薦內容"
                    # 試著從 body 取標題作為 alt text
                    if card_data.get('type') == 'bubble':
                        try:
                            body_contents = card_data.get('body', {}).get('contents', [])
                            for item in body_contents:
                                if item.get('type') == 'text' and item.get('weight') == 'bold':
                                    alt_title = item.get('text', alt_title)
                                    break
                        except Exception:
                            pass
                    messages.append(FlexMessage(alt_text=alt_title, contents=flex_container))
                except Exception as fe:
                    logger.error({"msg": "Failed to parse native LINE Flex JSON, fallback to text", "error": str(fe)})
                    has_card = False
            else:
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
                        # Ensure text matches label for consistent UX
                        # (user sees label on button, text appears in chat when clicked)
                        if len(label) > 20:
                            label = label[:17] + "..."
                        btn_text = btn.get('text', '') or ''
                        if not btn_text.strip() or btn_text == '點擊後傳送的文字':
                            btn_text = label
                        if len(btn_text) > 20:
                            btn_text = btn_text[:20]
                        action = {
                            "type": "message",
                            "label": label,
                            "text": btn_text
                        }
                    footer_buttons.append({
                        "type": "button",
                        "style": "primary",
                        "color": "#4F46E5",
                        "height": "sm",
                        "action": action,
                        "margin": "xs"
                    })
                
                # 如果 AI 輸出的圖卡沒有帶按鈕，且有傳入預設的建議引導按鈕，則在此填補
                if not footer_buttons and suggested_buttons:
                    for btn in suggested_buttons:
                        label = btn.get('label', '了解更多')
                        if len(label) > 20:
                            label = label[:17] + "..."
                        btn_text = btn.get('text', label)
                        if len(btn_text) > 20:
                            btn_text = btn_text[:20]
                        uri = btn.get('uri')
                        btn_action = {}
                        if uri:
                            btn_action = {"type": "uri", "label": label, "uri": uri}
                        else:
                            btn_action = {"type": "message", "label": label, "text": btn_text}
                        footer_buttons.append({
                            "type": "button",
                            "style": "primary",
                            "color": "#4F46E5",
                            "height": "sm",
                            "action": btn_action,
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
                # ── 自動為純文字 Flex Message 生成 footer 按鈕 ──
                footer_contents = []
                
                # 如果有傳入建議按鈕，轉換成 LINE 按鈕
                if suggested_buttons:
                    for btn in suggested_buttons:
                        label = btn.get('label', '了解更多')
                        if len(label) > 20:
                            label = label[:17] + "..."
                        btn_text = btn.get('text', label)
                        if len(btn_text) > 20:
                            btn_text = btn_text[:20]
                        uri = btn.get('uri')
                        
                        btn_action = {}
                        if uri:
                            btn_action = {
                                "type": "uri",
                                "label": label,
                                "uri": uri
                            }
                        else:
                            btn_action = {
                                "type": "message",
                                "label": label,
                                "text": btn_text
                            }
                            
                        footer_contents.append({
                            "type": "button",
                            "style": "primary",
                            "color": "#4F46E5",
                            "height": "sm",
                            "action": btn_action,
                            "margin": "xs"
                        })
                        
                # 加上提示語與分隔線
                if footer_contents:
                    footer_contents.insert(0, {
                        "type": "separator",
                        "color": "#F3F4F6",
                        "margin": "sm"
                    })
                    footer_contents.append({
                        "type": "separator",
                        "color": "#F3F4F6",
                        "margin": "md"
                    })
                
                footer_contents.append({
                    "type": "text",
                    "text": "💡 提示：本訊息由本地 AI 依據知識庫自動整理生成",
                    "size": "xxs",
                    "color": "#9CA3AF",
                    "margin": "md",
                    "align": "center"
                })

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
                        "text": main_text,
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
                    "contents": footer_contents,
                    "paddingAll": "sm"
                  }
                }
                flex_container = FlexContainer.from_json(json.dumps(flex_json))
                alt_text = f"AI 回覆：{ai_reply[:30]}..."
                messages.append(FlexMessage(alt_text=alt_text, contents=flex_container))
        
        # 4. 發送所有回覆 (支援超時/無效 reply_token 自動降級為 Push Message)
        if messages:
            use_push = force_push or not reply_token
            if use_push:
                if user_id:
                    logger.info("Using LINE Push Message API instead of reply token.")
                    await AsyncMessagingApi(api_client).push_message_with_http_info(
                        PushMessageRequest(
                            to=user_id,
                            messages=messages[:5]
                        )
                    )
                    logger.info("Advanced LINE Push Message sent successfully!")
                else:
                    logger.error("Push Message requested but user_id is missing.")
            else:
                try:
                    await AsyncMessagingApi(api_client).reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=reply_token,
                            messages=messages[:5]
                        )
                    )
                    logger.info("Advanced LINE reply sent successfully!")
                except Exception as ex:
                    err_str = str(ex)
                    if "reply token" in err_str.lower() or "400" in err_str:
                        if user_id:
                            logger.info("Reply token failed. Falling back to Push Message.")
                            await AsyncMessagingApi(api_client).push_message_with_http_info(
                                PushMessageRequest(
                                    to=user_id,
                                    messages=messages[:5]
                                )
                            )
                            logger.info("Fallback Advanced LINE Push Message sent successfully!")
                        else:
                            raise ex
                    else:
                        raise ex
                        
    except Exception as ex:
        logger.error({"msg": "Failed to send advanced Flex Message, falling back to TextMessage", "error": str(ex)})
        try:
            use_push = force_push or not reply_token
            if use_push:
                if user_id:
                    await AsyncMessagingApi(api_client).push_message_with_http_info(
                        PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text=ai_reply)]
                        )
                    )
                else:
                    logger.error("Push fallback requested but user_id is missing.")
            else:
                try:
                    await AsyncMessagingApi(api_client).reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=reply_token,
                            messages=[TextMessage(text=ai_reply)]
                        )
                    )
                except Exception as e2:
                    err_str = str(e2)
                    if ("reply token" in err_str.lower() or "400" in err_str) and user_id:
                        logger.info("Fallback reply token failed. Falling back to Push Message.")
                        await AsyncMessagingApi(api_client).push_message_with_http_info(
                            PushMessageRequest(
                                to=user_id,
                                messages=[TextMessage(text=ai_reply)]
                            )
                        )
                    else:
                        raise e2
        except Exception as final_ex:
            logger.error({"msg": "Final fallback reply/push failed", "error": str(final_ex)})

# 用戶最後生成的圖片 URL 記錄，便於一鍵轉影片
user_last_image = TTLCache(maxsize=1000, ttl=3600)
user_image_lock = threading.Lock()

async def reply_text(company: dict, reply_token: str, text: str, user_id: str = None, force_push: bool = False):
    """簡便的純文字回覆輔助函數，支援超時降級 Push (非同步)"""
    try:
        config = Configuration(access_token=company['line_access_token'])
        async with AsyncApiClient(config) as api_client:
            use_push = force_push or not reply_token
            if use_push and user_id:
                await AsyncMessagingApi(api_client).push_message_with_http_info(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=text)]
                    )
                )
                logger.info("Pure text LINE Push Message sent successfully!")
            else:
                try:
                    await AsyncMessagingApi(api_client).reply_message_with_http_info(
                        ReplyMessageRequest(
                            reply_token=reply_token,
                            messages=[TextMessage(text=text)]
                        )
                    )
                    logger.info("Pure text LINE reply sent successfully!")
                except Exception as ex:
                    err_str = str(ex)
                    if ("reply token" in err_str.lower() or "400" in err_str) and user_id:
                        logger.info("Reply token failed (pure text). Falling back to Push Message.")
                        await AsyncMessagingApi(api_client).push_message_with_http_info(
                            PushMessageRequest(
                                to=user_id,
                                messages=[TextMessage(text=text)]
                            )
                        )
                        logger.info("Fallback pure text LINE Push Message sent successfully!")
                    else:
                        raise ex
    except Exception as e:
        logger.error({"msg": "reply_text failed", "error": str(e)})


async def handle_text_event(company: dict, user_id: str, reply_token: str, user_message: str):
    """RAG + LLM 推理 + LINE 回覆 (非同步)"""
    start_time = time.time()
    
    # ── 顯示輸入中動畫 ──
    # Note: LINE Bot accounts do NOT show read receipts to users (platform limitation).
    # show_loading_animation only shows a typing indicator bubble, not a read receipt.
    # We use 60 seconds (the max) to ensure the animation covers even slow LLM responses.
    # The animation is automatically cleared when a message is sent to the user.
    try:
        if company.get('line_access_token') and user_id:
            config = Configuration(access_token=company['line_access_token'])
            async with AsyncApiClient(config) as api_client:
                await AsyncMessagingApi(api_client).show_loading_animation(
                    ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=60)
                )
    except Exception as le:
        logger.warning("Failed to show loading animation: %s", str(le))
    
    # ── 攔截 AI 生圖與生片指令 ──
    msg_lower = user_message.lower().strip()
    if msg_lower.startswith("/draw ") or msg_lower.startswith("/畫圖 "):
        prompt = user_message[6:].strip()
        if not prompt:
            await reply_text(company, reply_token, "請輸入繪圖提示詞，例如：/draw 一隻戴著墨鏡的貓", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return
            
        try:
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent_dir not in sys.path:
                sys.path.append(parent_dir)
            from admin.hf_api import generate_image
            
            img_url = generate_image(prompt)
            if img_url:
                with user_image_lock:
                    user_last_image[user_id] = img_url
                
                use_push = (time.time() - start_time) >= 4.2
                config = Configuration(access_token=company['line_access_token'])
                async with AsyncApiClient(config) as api_client:
                    msgs = [
                        ImageMessage(original_content_url=img_url, preview_image_url=img_url),
                        TextMessage(text=f"✨ 這是為您生成的圖片！\n指令: {prompt}\n\n💡 提示：輸入「/video」或「/動起來」可以將此圖片轉為短影片喔！")
                    ]
                    if use_push:
                        await AsyncMessagingApi(api_client).push_message_with_http_info(
                            PushMessageRequest(to=user_id, messages=msgs)
                        )
                    else:
                        try:
                            await AsyncMessagingApi(api_client).reply_message_with_http_info(
                                ReplyMessageRequest(reply_token=reply_token, messages=msgs)
                            )
                        except Exception as ex:
                            if "reply token" in str(ex).lower() or "400" in str(ex):
                                await AsyncMessagingApi(api_client).push_message_with_http_info(
                                    PushMessageRequest(to=user_id, messages=msgs)
                                )
                            else:
                                raise ex
                return
            else:
                await reply_text(company, reply_token, "抱歉，圖片生成失敗，請稍候重試。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
                return
        except Exception as e:
            logger.error({"msg": "LINE /draw failed", "error": str(e)})
            await reply_text(company, reply_token, "抱歉，圖片生成服務目前不可用。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return
            
    elif msg_lower == "/video" or msg_lower == "/動起來":
        with user_image_lock:
            img_url = user_last_image.get(user_id)
        if not img_url:
            await reply_text(company, reply_token, "您最近沒有生成過圖片喔！請先使用「/draw 您的指令」生成一張圖片。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return
            
        try:
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent_dir not in sys.path:
                sys.path.append(parent_dir)
            from admin.hf_api import generate_video
            
            video_url = generate_video(img_url)
            if video_url:
                use_push = (time.time() - start_time) >= 4.2
                config = Configuration(access_token=company['line_access_token'])
                async with AsyncApiClient(config) as api_client:
                    msgs = [
                        VideoMessage(
                            original_content_url=video_url, 
                            preview_image_url=img_url,
                            tracking_id=f"vid_{user_id[:8]}"
                        ),
                        TextMessage(text="🎬 您的動態短影片已生成完成！")
                    ]
                    if use_push:
                        await AsyncMessagingApi(api_client).push_message_with_http_info(
                            PushMessageRequest(to=user_id, messages=msgs)
                        )
                    else:
                        try:
                            await AsyncMessagingApi(api_client).reply_message_with_http_info(
                                ReplyMessageRequest(reply_token=reply_token, messages=msgs)
                            )
                        except Exception as ex:
                            if "reply token" in str(ex).lower() or "400" in str(ex):
                                await AsyncMessagingApi(api_client).push_message_with_http_info(
                                    PushMessageRequest(to=user_id, messages=msgs)
                                )
                            else:
                                raise ex
                return
            else:
                await reply_text(company, reply_token, "抱歉，影片生成失敗（可能 Hugging Face 佇列擁擠），請稍候重試。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
                return
        except Exception as e:
            logger.error({"msg": "LINE /video failed", "error": str(e)})
            await reply_text(company, reply_token, "抱歉，影片生成服務目前不可用。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return

    # ── 語意快取：查詢是否有高相似度的快取回覆 ──
    cached_reply = await check_cache(company['id'], user_message)
    if cached_reply:
        logger.info({"msg": "Semantic cache hit, skipping LLM", "user_msg": user_message[:50]})
        # 儲存對話記錄（快取命中也要記錄）
        clean_cached = re.sub(r'\[FLEX_CARD\][\s\S]*?\[/FLEX_CARD\]', '', cached_reply).strip()
        clean_cached = re.sub(r'\[FLEX_CARD\][\s\S]*', '', clean_cached).strip()
        clean_cached = _strip_markdown(clean_cached)
        save_messages(company['id'], user_id, user_message, clean_cached)

        # 回覆 LINE
        config = Configuration(access_token=company['line_access_token'])
        async with AsyncApiClient(config) as api_client:
            await reply_with_flex_or_text(
                api_client, reply_token,
                company.get('name', 'AI 客服助理'),
                cached_reply,
                logo_url=company.get('logo_url'),
                user_id=user_id,
                force_push=((time.time() - start_time) >= 4.2)
            )
        return

    # ── 正常 RAG + LLM 對話流程 ──
    # 1. 搜尋知識庫
    docs = await search_knowledge(company['id'], user_message)

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
    assets_block += '    {"label": "按鈕上顯示的文字（用戶看到的）", "text": "點擊後傳送到聊天室的文字（建議與 label 相同，確保用戶體驗一致）", "uri": "點擊後開啟的網址，不開啟網址則不設定此欄位或設為 null"}\n'
    assets_block += '  ]\n'
    assets_block += "}\n"
    assets_block += "[/FLEX_CARD]\n"
    assets_block += "```\n"
    assets_block += "3. 進階選項（多樣化版面）：如果需要呈現多張橫向滑動的輪播卡片（Carousel）或是精美的不帶按鈕的靜態卡片（Silent Card），你可以直接在 `[FLEX_CARD]...[/FLEX_CARD]` 中輸出 LINE 原生官方 JSON 格式（即以 `{\"type\": \"bubble\"}` 或 `{\"type\": \"carousel\"}` 開頭的原生結構）。系統將會自動無縫解析，這能讓你的排版更豐富生動，不用被局限於上述的簡化按鈕圖卡中！\n"

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
            async with AsyncApiClient(config) as api_client:
                await reply_with_flex_or_text(api_client, reply_token, company.get('name', 'AI 客服助理'), ai_reply, logo_url=company.get('logo_url'), user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
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

    # 不論是否 strict_rag，皆套用統一的引導式排版與服務風格
    style_instruction = (
        "\n\n【引導式服務與回覆風格指令】\n"
        "1. 你的角色是親切且主動的服務人員。當用戶詢問的業務資訊較多、步驟較繁複時（例如：申請資格、補助辦法等），**絕對不要一次吐出所有長篇大論的文字**以防截斷與民眾疲勞。\n"
        "2. 請先以 1-2 句話簡短概述核心或開場，然後**必須自行拼裝互動圖卡 [FLEX_CARD]**，提供引導式按鈕（如「申請資格」、「補助項目」等關鍵字）。\n"
        "3. **關於對話層數與接續問答的彈性限制**：\n"
        "   - 我們建議核心問題盡量在 3 層引導對話內回答完畢，避免對話過於冗長。\n"
        "   - **但是，這並非死板硬性的硬限制。** 如果用戶在 3 層後繼續追問、提出新問題或進行接續問答，你必須秉持親切主動的服務態度，**繼續正常提供 [FLEX_CARD] 互動按鈕引導**，不可在 3 層後退回為沒有 flex 樣式的純文字或拒絕提供按鈕。\n"
        "   - 無論對話進行到第幾層，只要還有延伸的相關業務或步驟，你都應適時使用 [FLEX_CARD] 來提供按鈕選項。\n"
        "4. 按鈕格式必須為：'label' 為按鈕顯示的文字，'text' 為用戶點擊後會自動發送回聊天室的文字。例如：\n"
        "```json\n"
        "[FLEX_CARD]\n"
        "{\n"
        "  \"imageUrl\": null,\n"
        "  \"title\": \"業務申請指引\",\n"
        "  \"text\": \"阿全村長為您整理了相關步驟，請點擊下方按鈕了解詳情：\",\n"
        "  \"buttons\": [\n"
        "    {\"label\": \"👉 申請資格\", \"text\": \"低收申請資格\"},\n"
        "    {\"label\": \"💰 補助項目\", \"text\": \"低收補助項目\"},\n"
        "    {\"label\": \"📄 應備文件\", \"text\": \"低收應備文件\"}\n"
        "  ]\n"
        "}\n"
        "[/FLEX_CARD]\n"
        "```\n"
        "5. 回覆時，除了 [FLEX_CARD] 標籤內部，外部文字嚴禁保留任何 markdown 符號（如 ** 或 #），請保持排版乾淨、語氣溫和。\n"
        "6. 【嚴格禁止直接輸出文件清單或條列規定】：\n"
        "   當用戶詢問的是某個「福利項目名稱」（如「嬰幼兒福利」、「育兒津貼」、「補助項目」等），\n"
        "   **絕對禁止**在第一層回覆中直接貼出文件清單或一長串條列規定。\n"
        "   正確做法：先用 1 句話說明該福利「是什麼」，再用 [FLEX_CARD] 提供 [詳細說明 / 申請資格 / 所需文件 / 補助金額] 等按鈕讓用戶主動選擇深度，\n"
        "   只有當用戶明確點擊「應備文件」或「所需文件」後，才可以列出文件清單。"
    )
    system_prompt += style_instruction


    # 4. 組合 messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 5. 呼叫本地 LLM
    try:
        resp = await llm_client.chat.completions.create(
            model=LOCAL_LLM_MODEL,
            messages=messages,
            temperature=0.3, # lower temperature for factual RAG matching
            max_tokens=1024,  # Increased to 1024 to allow full RAG reasoning and complete responses without truncation
            timeout=15.0
        )
        ai_reply = resp.choices[0].message.content

        # 1a. Fallback: some models (e.g. reasoning-distilled, uncensored variants) put
        #     the actual response in reasoning_content instead of content
        if not ai_reply or not ai_reply.strip():
            reasoning = getattr(resp.choices[0].message, 'reasoning_content', None)
            if reasoning and reasoning.strip():
                logger.info({"msg": "Using reasoning_content as fallback (content was empty)"})
                ai_reply = reasoning

        # 1b. Strip <think>...</think> blocks (reasoning models leak their chain-of-thought here)
        if ai_reply:
            ai_reply = re.sub(r'<think>[\s\S]*?</think>', '', ai_reply, flags=re.IGNORECASE).strip()

        # 2. Defensive check: if content is still empty after all fallbacks and stripping
        if not ai_reply or not ai_reply.strip():
            logger.warning({"msg": "LLM returned empty content after all fallbacks", "model": "local-model"})
            ai_reply = "抱歉，AI 回應異常（模型未產生有效回覆），請稍候重試或換個方式詢問。"

        # 3. 取得乾淨的文字紀錄，用來儲存至對話歷史 (不含 FLEX_CARD JSON 且去除 Markdown)
        clean_reply = re.sub(r'\[FLEX_CARD\][\s\S]*?\[/FLEX_CARD\]', '', ai_reply).strip()
        clean_reply = re.sub(r'\[FLEX_CARD\][\s\S]*', '', clean_reply).strip()
        clean_reply = _strip_markdown(clean_reply)
    except openai.APITimeoutError:
        logger.error({"msg": "LLM request timeout after 15s"})
        ai_reply = "抱歉，AI 服務暫時無法使用，請稍後再試。"
        clean_reply = ai_reply
    except openai.APIConnectionError as e:
        logger.error({"msg": "LLM connection failed (local server may be down)", "error": str(e)})
        ai_reply = "抱歉，AI 服務暫時無法使用，請稍後再試。"
        clean_reply = ai_reply
    except Exception as e:
        logger.error({"msg": "LLM request failed", "error": str(e)})
        ai_reply = "抱歉，AI 服務暫時無法使用，請稍後再試。"
        clean_reply = ai_reply

    # 6. 儲存對話記錄
    save_messages(company['id'], user_id, user_message, clean_reply)

    # 將成功的 LLM 回覆寫入語意快取（供後續相似問題命中）
    if ai_reply and ai_reply != "抱歉，AI 服務暫時無法使用，請稍後再試。":
        try:
            await add_to_cache(company['id'], user_message, ai_reply)
        except Exception as cache_err:
            logger.warning({"msg": "Failed to add to semantic cache", "error": str(cache_err)})

    # 7. 回覆 LINE
    # RAG docs 推薦引導按鈕推導 (作為 AI 沒有主動產出 FLEX_CARD 時的 Fallback 推薦按鈕)
    suggested_buttons = []
    if docs:
        query_prefix = user_message[:10].strip()
        combined_content = " ".join(d['content'] for d in docs)
        has_docs = any(kw in combined_content for kw in ['文件', '戶籍謄本', '證明', '身分證', '申請書', '應備', '所需'])
        has_qualifications = any(kw in combined_content for kw in ['資格', '條件', '符合', '標準', '門檻'])
        has_amount = any(kw in combined_content for kw in ['金額', '補助', '元', '津貼', '費用'])

        if has_qualifications:
            suggested_buttons.append({"label": "✅ 申請資格", "text": f"{query_prefix} 申請資格"})
        if has_docs and len(suggested_buttons) < 3:
            suggested_buttons.append({"label": "📄 應備文件", "text": f"{query_prefix} 應備文件"})
        if has_amount and len(suggested_buttons) < 3:
            suggested_buttons.append({"label": "💰 補助金額", "text": f"{query_prefix} 補助金額"})
            
        if len(docs) >= 2 and len(suggested_buttons) < 3:
            second_title = docs[1]['title']
            if second_title != docs[0]['title']:
                suggested_buttons.append({"label": f"🔍 {second_title[:12]}", "text": second_title[:20]})
        suggested_buttons = suggested_buttons[:3]

    # Use regular reply logic (force_push=False) and rely on the internal fallback mechanism:
    config = Configuration(access_token=company['line_access_token'])
    async with AsyncApiClient(config) as api_client:
        await reply_with_flex_or_text(api_client, reply_token, company.get('name', 'AI 客服助理'), ai_reply, logo_url=company.get('logo_url'), user_id=user_id, force_push=False, suggested_buttons=suggested_buttons)


async def handle_quick_summary_postback(company: dict, user_id: str, query_text: str):
    """
    方案 B+C：針對圖文選單 Postback 的快速摘要回應模式 (非同步)

    不跑 LLM（省去 3~10 秒等待），直接:
    1. 用 RAG 搜尋出最相關的 1~3 筆知識庫條目
    2. 從第一筆條目取出前 80 字作為即時摘要
    3. 組裝成 Flex 摘要卡片 + 最多 3 個引導按鈕 Push 給用戶

    用戶點按鈕後才觸發完整 LLM 詳細回答 (handle_text_event)。
    整體回應時間 < 1.5 秒。
    """
    docs = await search_knowledge(company['id'], query_text, limit=4)

    config = Configuration(access_token=company['line_access_token'])
    async with AsyncApiClient(config) as api_client:
        api = AsyncMessagingApi(api_client)

        if not docs:
            # 知識庫無資料，fallback 到完整 LLM 流程
            logger.info("Quick summary: no RAG docs found, falling back to LLM for: %s", query_text)
            await handle_text_event(company, user_id, None, query_text)
            return

        # ── 組裝摘要卡 ──
        primary = docs[0]
        topic_title = primary['title']

        # 從第一條取前 90 字當摘要；若有多筆，列出標題作為子項目
        summary_text = primary['content'][:90].rstrip() + '…'

        # 衍生的引導按鈕（從各筆 docs 標題推導）
        # 按鈕 1：讓用戶選「詳細說明」
        # 按鈕 2：讓用戶選「申請資格 / 所需文件」（依關鍵字判斷）
        # 按鈕 3 (若有第二筆)：以第二筆標題作為延伸問答入口
        buttons = []

        detail_label = "📋 查看詳細說明"
        detail_text = f"{query_text} 詳細說明"
        buttons.append({"label": detail_label, "text": detail_text})

        # 推導常見的「文件 / 資格 / 補助」衍生按鈕
        content_lower = primary['content'].lower()
        combined_content = " ".join(d['content'] for d in docs)
        has_docs = any(kw in combined_content for kw in ['文件', '戶籍謄本', '證明', '身分證', '申請書'])
        has_qualifications = any(kw in combined_content for kw in ['資格', '條件', '符合', '標準', '門檻'])
        has_amount = any(kw in combined_content for kw in ['金額', '補助', '元', '津貼', '費用'])

        if has_qualifications:
            buttons.append({"label": "✅ 申請資格", "text": f"{query_text[:10]} 申請資格"})
        if has_docs and len(buttons) < 3:
            buttons.append({"label": "📄 應備文件", "text": f"{query_text[:10]} 應備文件"})
        if has_amount and len(buttons) < 3:
            buttons.append({"label": "💰 補助金額", "text": f"{query_text[:10]} 補助金額"})

        # 若衍生按鈕不足，加上第二筆知識庫條目的標題作為探索入口
        if len(docs) >= 2 and len(buttons) < 3:
            second_title = docs[1]['title']
            if second_title != topic_title:
                buttons.append({"label": f"🔍 {second_title[:10]}", "text": second_title[:20]})

        # LINE Flex Card 限制：按鈕最多 3 個
        buttons = buttons[:3]

        # 組裝 Flex JSON
        footer_btns = []
        for btn in buttons:
            footer_btns.append({
                "type": "button",
                "style": "primary",
                "color": "#4F46E5",
                "height": "sm",
                "margin": "xs",
                "action": {
                    "type": "message",
                    "label": btn["label"],
                    "text": btn["text"]
                }
            })

        bubble = {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#4F46E5",
                "paddingAll": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": topic_title,
                        "color": "#FFFFFF",
                        "weight": "bold",
                        "size": "md",
                        "wrap": True
                    }
                ]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "paddingAll": "lg",
                "contents": [
                    {
                        "type": "text",
                        "text": summary_text,
                        "wrap": True,
                        "size": "sm",
                        "color": "#374151"
                    }
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "xs",
                "paddingAll": "md",
                "contents": footer_btns if footer_btns else [
                    {
                        "type": "text",
                        "text": "💡 請點擊上方按鈕或直接輸入問題",
                        "size": "xxs",
                        "color": "#9CA3AF",
                        "align": "center"
                    }
                ]
            }
        }

        flex_container = FlexContainer.from_json(json.dumps(bubble))
        flex_msg = FlexMessage(alt_text=f"📋 {topic_title} 快速摘要", contents=flex_container)

        try:
            await api.push_message_with_http_info(
                PushMessageRequest(to=user_id, messages=[flex_msg])
            )
            logger.info("Quick summary card sent for query: %s", query_text)
        except Exception as e:
            logger.error("Failed to send quick summary card: %s", str(e))
            # Fallback 到完整 LLM 流程
            await handle_text_event(company, user_id, None, query_text)


async def handle_postback_event(company: dict, user_id: str, reply_token: str, postback_data: str):
    """處理 LINE Postback 事件（圖文選單連動智慧客服） (非同步)"""
    logger.info(f"Received Postback data: {postback_data} for user: {user_id}")
    
    try:
        # 解析 query string 格式 (例如 action=rag_query&text=老莫介紹)
        params = urllib.parse.parse_qs(postback_data)
        action = params.get('action', [''])[0]
    except Exception:
        action = ''
        
    # 圖文選單觸發的 RAG 查詢 → 走快速摘要模式 (方案 B+C)
    # 只有在用戶主動在聊天室輸入文字時才跑完整 LLM 流程
    if action == 'rag_query':
        query_text = params.get('text', [''])[0]
        if query_text:
            logger.info("Quick summary postback for: '%s'", query_text)
            await handle_quick_summary_postback(company, user_id, query_text)
            return
            
    # 備用連動：若不含特定的 action，但 data 整串是有意義的非空文字
    if postback_data and not action:
        logger.info("Quick summary via raw postback data: '%s'", postback_data)
        await handle_quick_summary_postback(company, user_id, postback_data)
        return
        
    # 其他未定義 postback 動作 of 預設回覆
    await reply_text(company, reply_token, "已收到選單按鈕指令。", user_id=user_id)



# ============================================
# 路由：多租戶 Webhook 入口（單一路由，乾淨正確）
# ============================================

@app.post("/callback/{company_slug}")
async def callback(company_slug: str, request: Request, background_tasks: BackgroundTasks):
    """
    每間公司擁有獨立的 Webhook URL：
      https://lmbot.pingpower.com.tw/callback/{company_slug}
    """
    # 1. 查詢公司設定
    company = get_company(company_slug)
    if not company:
        logger.warning({"msg": "Unknown slug", "slug": company_slug})
        raise HTTPException(status_code=404, detail="Company not found")

    # 2. 驗證 LINE 簽名並解析事件（使用該公司的 channel secret 與 WebhookParser）
    signature = request.headers.get('X-Line-Signature', '')
    body_bytes = await request.body()
    body = body_bytes.decode('utf-8')

    parser = WebhookParser(company['line_channel_secret'])
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        logger.error({"msg": "Invalid LINE signature", "slug": company_slug})
        raise HTTPException(status_code=400, detail="Invalid signature")

    # 3. 處理事件 (使用背景執行緒以秒回 200 OK，防止 LINE 逾時)
    for event in events:
        if not hasattr(event, 'source') or not hasattr(event.source, 'user_id'):
            continue
        user_id = event.source.user_id
        reply_token = getattr(event, 'reply_token', None)

        # 處理文字訊息
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            background_tasks.add_task(
                handle_text_event,
                company, user_id, reply_token, event.message.text
            )
            
        # 處理 Postback 事件（支援圖文選單按鈕連動）
        elif hasattr(event, 'postback') and hasattr(event.postback, 'data'):
            background_tasks.add_task(
                handle_postback_event,
                company, user_id, reply_token, event.postback.data
            )

    return Response(content="OK", media_type="text/plain")


# ============================================
# 健康檢查端點（LINE Developers 的 Verify 按鈕用）
# ============================================

@app.get("/health")
def health():
    return JSONResponse(content={"status": "ok", "supabase": supabase is not None}, status_code=200)


# ============================================
# 靜態資源上傳目錄服務（支援 LINE 獲取 Logo & 自訂圖示）
# ============================================
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_FOLDER = os.path.join(PARENT_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.get("/uploads/{filename:path}")
def serve_upload(filename: str):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    abs_base = os.path.abspath(UPLOAD_FOLDER)
    abs_target = os.path.abspath(file_path)
    if not abs_target.startswith(abs_base):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


if __name__ == "__main__":
    logger.info("多租戶 LINE Bot 伺服器啟動：http://0.0.0.0:5000")
    logger.info("Webhook 格式：/callback/{company_slug}")
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="info")
