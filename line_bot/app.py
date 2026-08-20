import os
import re
import json
import logging
import time
import asyncio
import sys
import threading
import ast
import subprocess
from semantic_cache import check_cache, add_to_cache, invalidate_semantic_cache_by_text
import urllib.parse
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from starlette.concurrency import run_in_threadpool
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
background_tasks_set = set()
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="LM Bot API")

@app.on_event("startup")
async def startup_event():
    try:
        from embedding_model import EmbeddingModelSingleton
        await EmbeddingModelSingleton.preload_async()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to preload EmbeddingModelSingleton: {e}")

# Supabase 連線（使用 service_role key 才能繞過 RLS）
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase 連線已初始化")
else:
    logger.warning("未設定 SUPABASE_URL 或 SUPABASE_SERVICE_KEY，多租戶功能將停用")

# ── Webhook 介面：當資料庫發生更新時觸發快取清理與重新同步 ──
@app.post("/api/webhook/knowledge_update")
async def knowledge_update_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    當知識庫原始資料更新時，外部或 Supabase Webhook 呼叫此 API。
    JSON Payload:
    {
        "company_id": "...",
        "old_text": "...",  # 可選，用於主動作廢舊的快取
        "new_text": "..."   # 可選，用於主動作廢相關新快取
    }
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid JSON"})

    company_id = data.get("company_id")
    if not company_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing company_id"})

    old_text = data.get("old_text")
    new_text = data.get("new_text")

    # 1. 執行快取清理
    invalidated_count = 0
    if old_text:
        invalidated_count += await invalidate_semantic_cache_by_text(company_id, old_text)
    if new_text:
        invalidated_count += await invalidate_semantic_cache_by_text(company_id, new_text)

    # 2. 用 BackgroundTasks 非同步啟動 sync_embeddings.py 同步知識庫向量
    def run_sync():
        try:
            import sync_embeddings
            sync_embeddings.main()
            logger.info(f"Background task: sync_embeddings.py completed successfully.")
        except Exception as e:
            logger.error(f"Failed to run sync_embeddings.py background task: {e}")

    background_tasks.add_task(run_sync)
    logger.info(f"Triggered sync_embeddings.py in background via webhook. Invalidated {invalidated_count} cache entries.")

    return {
        "status": "success",
        "message": "Knowledge sync triggered in background.",
        "invalidated_cache_count": invalidated_count
    }

# ── 知識庫單筆修改 API (重生機制) ──
@app.post("/api/knowledge/update")
async def knowledge_update_api(request: Request, background_tasks: BackgroundTasks):
    """
    更新知識庫中的特定原始文章。
    JSON Payload:
    {
        "id": "...",          # 原始文章 ID
        "company_id": "...",  # 公司 ID
        "title": "...",       # 新標題
        "content": "...",     # 新內容
        "tags": [...]         # 新標籤
    }
    """
    if not supabase:
        return JSONResponse(status_code=500, content={"status": "error", "message": "Supabase client not initialized"})

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid JSON"})

    doc_id = data.get("id")
    company_id = data.get("company_id")
    title = data.get("title")
    content = data.get("content")
    tags = data.get("tags") or []

    if not doc_id or not company_id or not title or not content:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Missing required fields: id, company_id, title, content"})

    try:
        # 1. 查詢舊內容，用於作廢相關的語意快取
        def _get_old_doc():
            return supabase.table('knowledge_base').select('content').eq('id', doc_id).eq('company_id', company_id).execute()
        res = await run_in_threadpool(_get_old_doc)
        
        old_content = None
        if res.data and len(res.data) > 0:
            old_content = res.data[0].get('content')

        # 2. 清理相關快取 (雙向清理)
        invalidated_count = 0
        if old_content:
            invalidated_count += await invalidate_semantic_cache_by_text(company_id, old_content)
        invalidated_count += await invalidate_semantic_cache_by_text(company_id, content)

        # 3. 刪除所有基於這篇原始文章所切出的舊碎塊 (tags 包含 parent:ID)
        def _delete_old_chunks():
            parent_tag = f"parent:{doc_id}"
            return supabase.table('knowledge_base').delete().eq('company_id', company_id).contains('tags', [parent_tag]).execute()
        await run_in_threadpool(_delete_old_chunks)

        # 4. 更新原始文章：寫入新標題與內容，重設 is_active = True 並且清空舊的 embedding 準備重新訓練
        def _update_original():
            return supabase.table('knowledge_base').update({
                'title': title,
                'content': content,
                'tags': tags,
                'is_active': True,
                'embedding': None
            }).eq('id', doc_id).eq('company_id', company_id).execute()
        await run_in_threadpool(_update_original)

        def run_sync():
            try:
                import sync_embeddings
                sync_embeddings.main()
                logger.info(f"Knowledge update background sync completed successfully.")
            except Exception as e:
                logger.error(f"Failed to run sync_embeddings.py background task after manual update: {e}")

        background_tasks.add_task(run_sync)
        logger.info(f"Article {doc_id} updated. Invalidated {invalidated_count} caches. Triggered background training.")

        return {
            "status": "success",
            "message": "Article updated successfully. Background training and cache invalidation triggered.",
            "invalidated_cache_count": invalidated_count
        }

    except Exception as e:
        logger.error(f"Error during manual knowledge update: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Database operation failed: {str(e)}"})

# 本地 LLM 客戶端 (Async)
llm_client = AsyncOpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="sk-no-key-required"
)
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "local-model")


async def call_llm_with_retry(messages: list[dict], temperature: float = 0.2, max_tokens: int = 2048, timeout: float = 60.0, max_retries: int = 2):
    """呼叫本地 LLM 並具備防護與自動重試機制 (防止 APITimeoutError / APIConnectionError)"""
    for attempt in range(1, max_retries + 1):
        try:
            resp = await llm_client.chat.completions.create(
                model=LOCAL_LLM_MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout
            )
            return resp
        except (openai.APITimeoutError, openai.APIConnectionError) as e:
            logger.warning(f"LLM call attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                raise
            await asyncio.sleep(0.5 * attempt)

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


async def search_knowledge(company_id: str, query: str, limit: int = 3, required_tags: list[str] = None, query_embedding: list[float] = None) -> list[dict]:
    """使用 Supabase 混合檢索（Vector + FTS + RRF 融合評分，支援標籤過濾）(非同步)"""
    if not supabase:
        logger.warning("Supabase is not initialized. RAG skipped.")
        return []
    try:
        # 1. 使用極預算或生成 query embedding
        q_emb = query_embedding
        if not q_emb:
            q_emb = await get_embedding(query)
        if not q_emb:
            logger.warning("Failed to generate query embedding, returning empty search results.")
            return []
            
        # 2. 呼叫 supabase RPC "match_knowledge_hybrid"
        # 參數: query_embedding, query_text, match_count, company_filter, filter_tags
        def _do_rpc(tags):
            return supabase.rpc(
                'match_knowledge_hybrid',
                {
                    'query_embedding': q_emb,
                    'query_text': query,
                    'match_count': limit,
                    'company_filter': company_id,
                    'filter_tags': tags
                }
            ).execute()
        res = await run_in_threadpool(_do_rpc, required_tags)
        
        docs = res.data or []
        # Fallback: 若指定標籤後查無任何資料，自動降級為無標籤混合檢索，避免 LLM 標籤幻覺導致漏召回
        if not docs and required_tags:
            logger.info(f"Hybrid RAG search returned 0 docs with tags {required_tags}. Falling back to untagged search.")
            fallback_res = await run_in_threadpool(_do_rpc, None)
            docs = fallback_res.data or []

        logger.info(f"Hybrid RAG search found {len(docs)} documents (tags filter applied: {required_tags if docs and required_tags else 'None/Fallback'}).")
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


def get_user_profile(company_id: str, user_id: str) -> dict:
    """從 Supabase 取得使用者長期記憶 (profile_data)"""
    if not supabase:
        return {}
    try:
        result = (
            supabase.table('user_profiles')
            .select('profile_data')
            .eq('company_id', company_id)
            .eq('user_id', user_id)
            .execute()
        )
        if result.data:
            return result.data[0].get('profile_data', {})
    except Exception as e:
        logger.error({"msg": "get_user_profile failed", "user_id": user_id, "error": str(e)})
    return {}


def should_extract_memory(user_message: str) -> bool:
    """判斷使用者發問是否值得觸發 LLM 進行特徵記憶提取，以大幅節省 GPU 算力與佇列開銷。"""
    msg = (user_message or "").strip()
    if len(msg) < 4:
        return False
    
    # 常用純禮貌/操作型問候詞直接略過
    trivial_phrases = {
        '你好', '您好', '哈囉', '早安', '午安', '晚安', '謝謝', '感謝', '多謝',
        '好的', '了解', '收到', 'ok', 'yes', 'no', '拜拜', '再見', '在嗎', '嗨'
    }
    if msg.lower() in trivial_phrases:
        return False
        
    # 如果長度較短 (< 15 字)，且完全沒有包含任何個人第一人稱、居住地、偏好、身分特徵關鍵字，直接略過
    persona_indicators = [
        '我', '我們', '住', '歲', '家', '小孩', '兒', '女', '爸', '媽', '工作',
        '職業', '公司', '預算', '喜歡', '偏好', '想要', '需要', '會員', '買過', '訂購',
        '電話', '信箱', '地址', '推薦'
    ]
    if len(msg) < 15 and not any(kw in msg for kw in persona_indicators):
        return False

    return True


async def execute_memory_update_agent(company_id: str, user_id: str, user_message: str, current_profile: dict):
    """Memory Update Agent: 背景執行。使用 LLM 解析使用者輸入，提取新出現的個人特徵，並合併更新至 Supabase。"""
    # 預先節流篩選，避免無效問答佔用 GPU 推論佇列
    if not should_extract_memory(user_message):
        return

    # 限制 LLM 回覆必須是合法的 JSON 格式。
    prompt = (
        "你是一個個人特徵提取助理。\n"
        "請分析使用者的最新輸入，並從中提取出可能對未來對話有用的「個人特徵與背景資訊」（例如：居住地、年齡、家庭成員、子女數、職業、特定健康狀況或偏好）。\n\n"
        f"【目前的個人特徵 JSON】：\n{json.dumps(current_profile, ensure_ascii=False)}\n\n"
        f"【使用者最新輸入】：\n\"{user_message}\"\n\n"
        "【任務指引】：\n"
        "1. 如果使用者的輸入中「包含新的個人特徵或更新」，請將其與當前 JSON 合併。如果是已存在的特徵但有變化，請更新它。\n"
        "2. 如果使用者的輸入「不包含任何個人特徵」或只是普通的問答/打招呼，請直接回覆原來的 JSON，不要做任何更改。\n"
        "3. 你的回覆「必須只能是一個合法的 JSON 字串」，不要有任何 Markdown 標記（如 ```json ... ```），不要有任何解釋文字。\n"
        "4. 請保持 JSON 鍵值對簡明易懂（例如：{\"location\": \"台北\", \"membership_tier\": \"VIP\"}）。\n"
        "輸出範例：{\"location\": \"台北\", \"membership_tier\": \"VIP\"}"
    )
    
    try:
        resp = await llm_client.chat.completions.create(
            model=LOCAL_LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一個只會輸出合法 JSON 格式的特徵提取器。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=1024,
            timeout=30.0
        )
        choice = resp.choices[0]
        if choice.finish_reason == 'length':
            logger.warning("Memory update agent output truncated due to finish_reason == 'length' (max_tokens exhausted).")

        llm_output = choice.message.content.strip() if choice.message.content else ""
        
        # Reasoning Guard: strip <think>...</think> blocks including unclosed tags before parsing JSON
        llm_output = re.sub(r'<think>[\s\S]*?(?:</think>|$)', '', llm_output, flags=re.IGNORECASE).strip()
        if not llm_output:
            return

        # Regex extract JSON code block if wrapped in Markdown fenced code block
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', llm_output, flags=re.IGNORECASE)
        clean_json_str = match.group(1).strip() if match else llm_output

        new_profile = json.loads(clean_json_str)
        if isinstance(new_profile, dict) and new_profile != current_profile:
            def _do_upsert():
                return supabase.table('user_profiles').upsert({
                    "company_id": company_id,
                    "user_id": user_id,
                    "profile_data": new_profile,
                    "updated_at": "now()"
                }).execute()
            await run_in_threadpool(_do_upsert)
            logger.info(f"Updated user profile for user {user_id}: {new_profile}")
    except Exception as e:
        logger.warning(f"Failed to update user memory: {str(e)}")


async def execute_query_expansion_agent(user_message: str, history: list[dict]) -> dict:
    """Query Expansion & Routing Agent: 
    使用 LLM 展開查詢語句，並分析是否需要過濾特定的主題標籤。
    回傳字典: {"query": str, "tags": list[str] | None}
    """
    history_context = ""
    if history:
        history_context = "【最近對話上下文】：\n"
        for msg in history[-4:]:
            role_name = "用戶" if msg['role'] == 'user' else "AI"
            history_context += f"{role_name}：{msg['content']}\n"
    
    prompt = (
        "你是一個查詢重寫與標籤路由助理。\n"
        "請分析用戶的最新發問，結合上下文，將其展開成適合搜尋知識庫的長查詢句，並且判斷是否需要篩選特定的分類標籤（如：產品規格、服務收費、售後服務、使用教學、常見問題等）。\n\n"
        f"{history_context}\n"
        f"【用戶最新發問】：\"{user_message}\"\n\n"
        "【任務指引】：\n"
        "1. 請優先以 JSON 格式輸出：{\"query\": \"展開後的完整發問\", \"tags\": [\"標籤1\", \"標籤2\"]}，若無特定標籤請設為 null。\n"
        "2. 不要加上任何你的解釋、問候語、或引號；若無法輸出 JSON，直接輸出擴寫後的查詢文字本身亦可。\n"
        "3. 如果該發問已經足夠清晰具體（如「退換貨流程如何辦理」），請直接原樣輸出其 query，不要做無意義的展開。\n"
        "請直接輸出結果："
    )
    
    try:
        resp = await llm_client.chat.completions.create(
            model=LOCAL_LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一個查詢重寫與標籤路由助理。請優先以 JSON 格式 {\"query\": \"...\", \"tags\": [...]} 輸出，或直接輸出查詢語句，不做任何解釋。"},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=512,
            timeout=10.0
        )
        choice = resp.choices[0]
        content = choice.message.content.strip() if choice.message.content else ""
        raw_msg = choice.message
        
        if choice.finish_reason == 'length':
            logger.warning("Query expansion truncated due to finish_reason == 'length' (max_tokens exhausted).")

        if not content and hasattr(raw_msg, 'reasoning_content') and raw_msg.reasoning_content:
            logger.warning("Query expansion model only returned reasoning_content. Aborting to prevent thought leak.")
            return {"query": user_message, "tags": None}

        # Strip <think>...</think> blocks
        content = re.sub(r'<think>[\s\S]*?(?:</think>|$)', '', content, flags=re.IGNORECASE).strip()
        if not content:
            return {"query": user_message, "tags": None}

        # Extract JSON code block if wrapped in Markdown fenced block
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content, flags=re.IGNORECASE)
        clean_text = match.group(1).strip() if match else content

        try:
            parsed = json.loads(clean_text)
            if isinstance(parsed, dict):
                query_val = parsed.get("query", user_message)
                if not isinstance(query_val, str) or not query_val.strip():
                    query_val = user_message
                else:
                    query_val = query_val.strip()

                tags_val = parsed.get("tags")
                if isinstance(tags_val, str):
                    tags_val = [t.strip() for t in re.split(r'[,、\s]+', tags_val) if t.strip()]
                elif isinstance(tags_val, list):
                    tags_val = [str(t).strip() for t in tags_val if isinstance(t, str) and t.strip()]
                else:
                    tags_val = None

                if not tags_val:
                    tags_val = None

                return {"query": query_val, "tags": tags_val}
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # Plain text fallback
        expanded = re.sub(r'^["\'「]+|["\'」]+$', '', content).strip()
        if not expanded:
            expanded = user_message
        return {"query": expanded, "tags": None}
    except Exception as e:
        logger.warning(f"Failed to expand query: {str(e)}")
    return {"query": user_message, "tags": None}


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


def extract_suggested_options(ai_reply: str) -> list[dict]:
    """Parse the trailing suggested-options section from LLM reply.
    
    Looks for patterns like:
    👉 您可能還想知道：
    1. 方案費用說明
    2. 退換貨申請資格
    
    Returns a list of button dicts: [{"label": "...", "text": "..."}]
    """
    options = []
    if not ai_reply:
        return options
    
    # Find the options section marker
    markers = ['您可能還想知道', '還想了解', '可以進一步詢問', '延伸問題', '相關問題']
    marker_idx = -1
    for marker in markers:
        idx = ai_reply.find(marker)
        if idx != -1:
            marker_idx = idx
            break
    
    if marker_idx == -1:
        return options
    
    options_text = ai_reply[marker_idx:]
    # Match lines like "1. 育兒津貼申請資格" or "• 低收入戶補助"
    # Improved pattern: allow leading whitespace and more list markers (like * and +)
    pattern = r'^\s*(?:\d+[.、\s]|\d+\s|[•·\-\*+])\s*(.+)$'
    for match in re.finditer(pattern, options_text, re.MULTILINE):
        label = match.group(1).strip()
        if not label or len(label) < 2:
            continue
        btn_label = label[:17] + "..." if len(label) > 20 else label
        btn_text = label[:20]
        options.append({"label": btn_label, "text": btn_text})
    
    return options[:4]  # LINE allows max 4 buttons per card


def strip_options_section(ai_reply: str) -> str:
    """Remove the trailing suggested-options section from LLM reply for clean display."""
    if not ai_reply:
        return ai_reply
    markers = ['您可能還想知道', '還想了解', '可以進一步詢問', '延伸問題', '相關問題']
    for marker in markers:
        idx = ai_reply.find(marker)
        if idx != -1:
            # Go back to find the separator line (--- or 👉)
            prefix = ai_reply[:idx].rstrip()
            # Remove trailing separator lines
            lines = prefix.split('\n')
            while lines and lines[-1].strip() in ('---', '—', '👉', ''):
                lines.pop()
            return '\n'.join(lines).strip()
    return ai_reply


async def route_user_intent(company: dict, user_id: str, reply_token: str, user_message: str, start_time: float, query_embedding: list[float] | None = None) -> tuple[bool, list[float] | None]:
    """Router Agent: 判斷用戶意圖並進行分流。
    
    支援：
    1. 繪圖指令 (/draw)
    2. 動態影片生成 (/video, /動起來)
    3. 語意快取命中 (Semantic Cache Hit)
    
    回傳 (is_handled, q_emb)
    """
    msg_lower = user_message.lower().strip()
    
    # ── 1. 攔截 AI 生圖與生片指令 ──
    if msg_lower.startswith("/draw ") or msg_lower.startswith("/畫圖 "):
        prompt = user_message[6:].strip()
        if not prompt:
            await reply_text(company, reply_token, "請輸入繪圖提示詞，例如：/draw 一隻戴著墨鏡的貓", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return True, None
            
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
                return True, None
            else:
                await reply_text(company, reply_token, "抱歉，圖片生成失敗，請稍後重試。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
                return True, None
        except Exception as e:
            logger.error({"msg": "LINE /draw failed", "error": str(e)})
            await reply_text(company, reply_token, "抱歉，繪圖服務目前不可用。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return True, None
            
    if msg_lower.startswith("/video") or msg_lower.startswith("/動起來"):
        with user_image_lock:
            img_url = user_last_image.get(user_id)
        if not img_url:
            await reply_text(company, reply_token, "請先使用「/draw 描述」生成一張圖片後，才能將其轉為短影片喔！", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return True, None
            
        try:
            parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if parent_dir not in sys.path:
                sys.path.append(parent_dir)
            from admin.hf_api import generate_video_from_image
            
            video_url = generate_video_from_image(img_url)
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
                return True, None
            else:
                await reply_text(company, reply_token, "抱歉，影片生成失敗（可能 Hugging Face 佇列擁擠），請稍候重試。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
                return True, None
        except Exception as e:
            logger.error({"msg": "LINE /video failed", "error": str(e)})
            await reply_text(company, reply_token, "抱歉，影片生成服務目前不可用。", user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return True

    # ── 2. 語意快取：查詢是否有高相似度的快取回覆 ──
    # 優化：即使發問極短，依然允許進行「精確字串比對」(bypass_semantic=True)，大於等於 12 個字才允許進行「語意向量比對」
    cached_reply, q_emb = await check_cache(
        company['id'], 
        user_message, 
        bypass_semantic=(len(user_message) < 12),
        query_embedding=query_embedding
    )
    
    if cached_reply:
        logger.info({"msg": "Semantic cache hit, skipping LLM", "user_msg": user_message[:50]})
        
        # 解析快取中的推薦選項並進行文字剔除
        cached_buttons = extract_suggested_options(cached_reply)
        
        has_marker = False
        markers = ['您可能還想知道', '還想了解', '可以進一步詢問', '延伸問題', '相關問題']
        for marker in markers:
            if marker in cached_reply:
                has_marker = True
                break
                
        if has_marker:
            cached_reply_for_display = strip_options_section(cached_reply)
        else:
            cached_reply_for_display = cached_reply

        # 準備對話記錄的乾淨文字
        clean_cached = re.sub(r'\[FLEX_CARD\][\s\S]*?\[/FLEX_CARD\]', '', cached_reply_for_display).strip()
        clean_cached = re.sub(r'\[FLEX_CARD\][\s\S]*', '', clean_cached).strip()
        clean_cached = _strip_markdown(clean_cached)

        # 建立快取命中的引導按鈕 (RAG 未觸發所以使用 static_fallback)
        static_fallback = [
            {"label": "🔄 重新詢問", "text": "我想重新詢問"},
            {"label": "📞 聯繫服務窗口", "text": "聯繫窗口"}
        ]
        final_cached_buttons = cached_buttons or static_fallback
        final_cached_buttons = final_cached_buttons[:4]

        # 回覆 LINE
        config = Configuration(access_token=company['line_access_token'])
        async with AsyncApiClient(config) as api_client:
            await reply_with_flex_or_text(
                api_client, reply_token,
                company.get('name', 'AI 客服助理'),
                cached_reply_for_display,
                logo_url=company.get('logo_url'),
                user_id=user_id,
                force_push=((time.time() - start_time) >= 55.0),
                suggested_buttons=final_cached_buttons
            )
        
        # 儲存對話記錄移至背景任務執行 (加上 Exception Handling)
        async def safe_save_messages(*args):
            try:
                await run_in_threadpool(save_messages, *args)
            except Exception as e:
                logger.error({"msg": "Background save_messages failed", "error": str(e)})

        task = asyncio.create_task(safe_save_messages(company['id'], user_id, user_message, clean_cached))
        background_tasks_set.add(task)
        task.add_done_callback(background_tasks_set.discard)
        return True, q_emb

    return False, q_emb


async def execute_rag_qa_agent(company: dict, user_id: str, reply_token: str, user_message: str, start_time: float, query_embedding: list[float] | None = None):
    """RAG QA Agent: 執行知識庫檢索、LLM 生成、以及回覆 UI 設計的串接。"""
    # 1. 並行取得對話歷史與長期記憶 (User Profile)
    history, user_profile = await asyncio.gather(
        run_in_threadpool(get_history, company['id'], user_id, 3),
        run_in_threadpool(get_user_profile, company['id'], user_id)
    )

    # 限制對話歷史總字數不超過 1000 字，避免 Prefill 膨脹，但強制保留最新的一筆
    total_history_len = 0
    truncated_history = []
    for i, msg in enumerate(reversed(history or [])):
        content = msg.get("content") or ""
        # 最新的一筆訊息強制保留，如果太長則進行截斷
        if i == 0:
            if len(content) > 1000:
                msg = msg.copy()
                msg["content"] = content[:1000] + "..."
            content_len = len(msg.get("content") or "")
        else:
            content_len = len(content)
            if total_history_len + content_len > 1000:
                break
        
        truncated_history.append(msg)
        total_history_len += content_len
    history = list(reversed(truncated_history))

    # 2. 查詢重寫、展開與標籤預測 (Query Expansion & Intent Classifier)
    expansion_result = await execute_query_expansion_agent(user_message, history)
    search_query = expansion_result.get("query", user_message)
    required_tags = expansion_result.get("tags")
    logger.info({"msg": "Query expansion completed", "original": user_message[:50], "expanded": search_query[:50], "tags": required_tags})

    # 3. 並行搜尋知識庫 (RAG) 與載入公司圖文資產 (復用 query_embedding)
    def _do_select_assets():
        if not supabase:
            return None
        return supabase.table('company_assets').select('*').eq('company_id', company['id']).execute()

    docs_task = search_knowledge(company['id'], search_query, required_tags=required_tags, query_embedding=query_embedding)
    assets_task = run_in_threadpool(_do_select_assets)

    docs, assets_res = await asyncio.gather(docs_task, assets_task)
    assets = assets_res.data if (assets_res and hasattr(assets_res, 'data') and assets_res.data) else []

    # 3. 組合 system prompt 與檢查是否為 Strict RAG 模式
    original_prompt = company.get('system_prompt', '你是一個友善、專業的 AI 客服助理。')
    is_strict_rag = False

    if original_prompt.startswith('[STRICT_RAG]'):
        is_strict_rag = True
        system_prompt = original_prompt[len('[STRICT_RAG]'):].lstrip()
    else:
        system_prompt = original_prompt

    # 3.0 注入使用者長期記憶背景資訊
    if user_profile:
        profile_str = ", ".join(f"{k}: {v}" for k, v in user_profile.items())
        system_prompt += (
            f"\n\n【關於此用戶的已知個人背景資訊】：\n{profile_str}\n"
            "請在回答時，優先主動結合此背景資訊，提供個人化的回覆（例如：若已知用戶的特定需求、身分或偏好，應主動針對該條件提供精確推薦與回答）。"
        )

    context = ""
    if docs:
        raw_context = "\n\n".join(f"【{d['title']}】\n{d['content']}" for d in docs)
        if len(raw_context) > 2500:
            context = raw_context[:2500] + "\n\n（...部分資料已因長度限制截斷...）"
            logger.info(f"RAG context truncated from {len(raw_context)} to {len(context)} chars")
        else:
            context = raw_context

    if is_strict_rag:
        # 在 Strict RAG 模式下，如果 docs 為空（即知識庫沒有匹配到任何資料），直接拒絕回答，不呼叫 LLM 節省資源！
        if not docs:
            ai_reply = "抱歉，在我的知識庫中找不到與此問題相關的資訊。"
            await run_in_threadpool(save_messages, company['id'], user_id, user_message, ai_reply)

            # 回覆 LINE
            config = Configuration(access_token=company['line_access_token'])
            async with AsyncApiClient(config) as api_client:
                await reply_with_flex_or_text(api_client, reply_token, company.get('name', 'AI 客服助理'), ai_reply, logo_url=company.get('logo_url'), user_id=user_id, force_push=((time.time() - start_time) >= 4.2))
            return

        # 如果有匹配到資料，則在 system_prompt 加上極嚴格的安全限制
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
            system_prompt += f"\n\n以下是公司相關資料，請優先參考：\n\n{context}"

    company_contact = company.get('name', '相關單位')
    # Unified response style instruction — Rich, structured and clear answers
    style_instruction = (
        "\n\n【回覆規則 — 嚴格遵守（手機閱讀最佳化）】\n"
        "■ 語氣風格\n"
        "• 用親切、溫暖的口吻，像一個熟悉業務的好朋友在幫忙解答\n"
        "• 適度使用 emoji（如 ✅ 📌 💡）讓訊息更活潑，但不要過度堆疊\n"
        "• 可以用「您好～」「沒問題！」「這部分幫您整理一下～」等自然的開場\n"
        "• 結尾可加一句暖心的話，如「有任何問題隨時問我喔！」「希望有幫到您～」\n"
        "• 嚴禁機械式回答，要讓用戶感覺在跟「人」對話而非機器\n\n"
        "■ 精簡直答\n"
        "• 開場後直接給出結論或答案，不要鋪陳\n"
        "• 用 • 條列最多 3 個關鍵重點（含具體數值：金額、日期、資格條件）\n"
        f"• 若資料不足，友善告知「這部分建議直接打給 {company_contact} 確認比較準確喔～」\n"
        "• 全文控制在 200 字以內（不含引導選項），寧可精簡也不要冗長\n\n"
        "■ 格式規範\n"
        "• 重要數字或名稱用【】標示\n"
        "• 嚴禁使用 Markdown 語法（#, **, `）\n"
        "• 不要重複用戶問題、不要說「讓我為您查詢」\n\n"
        "■ 引導選項（每次必須提供）\n"
        "回覆結尾附上：\n"
        "---\n"
        "👉 您可能還想知道：\n"
        "1. [相關選項A]\n"
        "2. [相關選項B]\n"
        "3. [相關選項C]\n\n"
        "選項必須與當前話題直接相關。\n"
    )
    system_prompt += style_instruction


    # 4. 組合 messages
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # 5. 呼叫本地 LLM (max_tokens=600 配合精簡回覆風格)
    try:
        resp = await call_llm_with_retry(
            messages=messages,
            temperature=0.2,
            max_tokens=600,
            timeout=45.0
        )
        choice = resp.choices[0]
        finish_reason = choice.finish_reason
        logger.info(f"DEBUG LLM Raw: choices={resp.choices} | finish_reason={finish_reason}")
        ai_reply = choice.message.content or ""
        
        # 清除 <think> 標籤 (若有)
        ai_reply = re.sub(r'<think>[\s\S]*?(?:</think>|$)', '', ai_reply, flags=re.IGNORECASE).strip()
        if "[FINAL_ANSWER]" in ai_reply:
            ai_reply = ai_reply.split("[FINAL_ANSWER]")[-1].strip()

        # 2. Defensive check: if content is still empty
        if not ai_reply or not ai_reply.strip():
            if docs and len(docs) > 0:
                logger.warning({"msg": "LLM output empty, using RAG docs fallback summary", "model": LOCAL_LLM_MODEL})
                doc_summary = "\n• ".join([d['title'] for d in docs[:3]])
                first_content = docs[0]['content'][:180].replace('\n', ' ')
                ai_reply = f"根據知識庫為您查詢到相關資訊：\n\n【{docs[0]['title']}】\n{first_content}...\n\n---\n👉 您可能還想知道：\n1. 檢視 {docs[0]['title'][:10]}\n2. 聯繫客服服務"
            else:
                logger.warning({"msg": "LLM returned empty content after all fallbacks", "model": LOCAL_LLM_MODEL})
                ai_reply = "抱歉，AI 服務暫時無法處理您的問題，請換個方式詢問，或稍後再試。"

        # 3. 取得乾淨的文字紀錄 (不含 FLEX_CARD JSON 且去除 Markdown)
        clean_reply = re.sub(r'\[FLEX_CARD\][\s\S]*?\[/FLEX_CARD\]', '', ai_reply).strip()
        clean_reply = re.sub(r'\[FLEX_CARD\][\s\S]*', '', clean_reply).strip()
        clean_reply = _strip_markdown(clean_reply)
    except openai.APITimeoutError:
        logger.error({"msg": "LLM request timeout after 60s"})
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

    # 6. 先由 UI/UX 代理人接手進行畫面的格式化與回覆發送給用戶
    await execute_ui_ux_agent(
        company, user_id, reply_token, 
        ai_reply, clean_reply, docs, 
        user_message, start_time
    )

    # 7. 將對話記錄與語意快取寫入移至背景非同步執行，不阻塞用戶回應
    task1 = asyncio.create_task(
        _async_save_and_cache(
            company['id'], user_id, user_message, clean_reply, ai_reply, user_profile, query_embedding
        )
    )
    background_tasks_set.add(task1)
    task1.add_done_callback(background_tasks_set.discard)

    # 8. 在背景非同步啟動記憶更新 (Memory Update Agent)
    task2 = asyncio.create_task(
        execute_memory_update_agent(
            company['id'], user_id, user_message, user_profile
        )
    )
    background_tasks_set.add(task2)
    task2.add_done_callback(background_tasks_set.discard)


async def execute_ui_ux_agent(
    company: dict, 
    user_id: str, 
    reply_token: str, 
    ai_reply: str, 
    clean_reply: str, 
    docs: list, 
    user_message: str, 
    start_time: float
):
    """UI/UX Designer Agent: 將純文字的 AI 回覆組裝成 LINE 的 Flex Message/引導按鈕，並發送給使用者。"""
    # === Button double-insurance: extract from LLM reply first, then RAG fallback ===
    
    # Priority 1: Extract suggested options from LLM's reply text
    llm_buttons = extract_suggested_options(ai_reply)
    
    # 無論是否有成功解析出按鈕，只要有 marker，就一律將選項文字區塊剔除，避免顯示無法點擊 the 文字
    has_marker = False
    markers = ['您可能還想知道', '還想了解', '可以進一步詢問', '延伸問題', '相關問題']
    for marker in markers:
        if marker in ai_reply:
            has_marker = True
            break
            
    if has_marker:
        ai_reply_for_display = strip_options_section(ai_reply)
    else:
        ai_reply_for_display = ai_reply
     
    # Priority 2: RAG docs keyword-based suggested buttons
    rag_buttons = []
    if docs:
        query_prefix = user_message[:10].strip()
        combined_content = " ".join(d['content'] for d in docs)
        has_docs = any(kw in combined_content for kw in ['文件', '資料', '證明', '身分證', '申請書', '應備', '所需', '必備', '準備'])
        has_qualifications = any(kw in combined_content for kw in ['資格', '條件', '符合', '標準', '門檻', '適用', '對象'])
        has_amount = any(kw in combined_content for kw in ['金額', '費用', '價格', '收費', '補助', '元', '津貼', '定價', '方案'])
        has_process = any(kw in combined_content for kw in ['流程', '步驟', '如何申請', '如何購買', '退換貨', '維修', '保固', '退費'])
        has_booking = any(kw in combined_content for kw in ['營業時間', '門市', '分店', '預約', '掛號', '訂位', '地址', '位置'])
 
        if has_qualifications:
            rag_buttons.append({"label": "✅ 適用條件", "text": f"{query_prefix} 適用條件"})
        if has_amount and len(rag_buttons) < 3:
            rag_buttons.append({"label": "💰 費用說明", "text": f"{query_prefix} 費用說明"})
        if has_process and len(rag_buttons) < 3:
            rag_buttons.append({"label": "🔄 服務流程", "text": f"{query_prefix} 服務流程"})
        if has_booking and len(rag_buttons) < 3:
            rag_buttons.append({"label": "📍 營業與預約", "text": f"{query_prefix} 營業與預約"})
        if has_docs and len(rag_buttons) < 3:
            rag_buttons.append({"label": "📄 準備資料", "text": f"{query_prefix} 準備資料"})
             
        if len(docs) >= 2 and len(rag_buttons) < 3:
            second_title = docs[1]['title']
            if second_title != docs[0]['title']:
                rag_buttons.append({"label": f"🔍 {second_title[:12]}", "text": second_title[:20]})
        rag_buttons = rag_buttons[:3]
 
    # Priority 3: Static fallback buttons
    static_fallback = [
        {"label": "🔄 重新詢問", "text": "我想重新詢問"},
        {"label": "📞 聯繫服務窗口", "text": "聯繫窗口"}
    ]
     
    # Merge with priority: LLM-extracted > RAG-derived > static fallback
    final_buttons = llm_buttons or rag_buttons or static_fallback
    final_buttons = final_buttons[:4]
 
    # Send reply
    config = Configuration(access_token=company['line_access_token'])
    async with AsyncApiClient(config) as api_client:
        await reply_with_flex_or_text(
            api_client, reply_token,
            company.get('name', 'AI 客服助理'),
            ai_reply_for_display,
            logo_url=company.get('logo_url'),
            user_id=user_id,
            force_push=((time.time() - start_time) >= 55.0),
            suggested_buttons=final_buttons
        )


async def _show_loading_async(access_token: str, user_id: str):
    """非同步觸發 LINE 載入中動畫」"""
    try:
        config = Configuration(access_token=access_token)
        async with AsyncApiClient(config) as api_client:
            await AsyncMessagingApi(api_client).show_loading_animation(
                ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=60)
            )
    except Exception as e:
        logger.warning(f"Async loading animation error: {e}")


async def _async_save_and_cache(company_id: str, user_id: str, user_message: str, clean_reply: str, ai_reply: str, user_profile: dict | None, query_embedding: list[float] | None):
    """背景非同步執行對話存檔與語意快取寫入，避免讓使用者等待」"""
    try:
        await run_in_threadpool(save_messages, company_id, user_id, user_message, clean_reply)
        is_cacheable = (
            len(user_message) >= 12 and
            ai_reply and
            ai_reply != "抱歉，AI 服務暫時無法使用，請稍後再試。" and
            not user_profile
        )
        if is_cacheable:
            await add_to_cache(company_id, user_message, ai_reply, query_embedding=query_embedding)
            logger.info(f"Background cached query: '{user_message}'")
    except Exception as e:
        logger.warning(f"Background save_messages/add_to_cache failed: {e}")


async def handle_text_event(company: dict, user_id: str, reply_token: str, user_message: str):
    """多代理人協調者 (Orchestrator): 協調執行 LINE 文字事件的處理流程 (非同步)"""
    start_time = time.time()
    
    try:
        # 1. Router Agent 進行意圖路由與分流（處理畫圖、影片或快取命中）
        is_handled, q_emb = await route_user_intent(company, user_id, reply_token, user_message, start_time)
        if is_handled:
            return
            
        # ── 僅在快取未命中、確定要進入 LLM 時才在背景顯示輸入中動畫 ──
        try:
            if company.get('line_access_token') and user_id:
                asyncio.create_task(_show_loading_async(company['line_access_token'], user_id))
        except Exception as le:
            logger.warning("Failed to show loading animation: %s", str(le))

        # 2. 執行 RAG QA Agent 處理知識庫檢索與 LLM 回覆
        await execute_rag_qa_agent(company, user_id, reply_token, user_message, start_time, query_embedding=q_emb)
    
    except Exception as e:
        logger.error({"msg": "Global unhandled exception in handle_text_event", "error": str(e)}, exc_info=True)
        try:
            if company.get('line_access_token') and user_id:
                config = Configuration(access_token=company['line_access_token'])
                async with AsyncApiClient(config) as api_client:
                    api = AsyncMessagingApi(api_client)
                    await api.push_message(
                        PushMessageRequest(
                            to=user_id,
                            messages=[TextMessage(text="抱歉，系統目前遇到了一點異常，請稍後再試。")]
                        )
                    )
        except Exception as fallback_e:
            logger.error({"msg": "Failed to send fallback error message", "error": str(fallback_e)})


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
        has_docs = any(kw in combined_content for kw in ['文件', '資料', '證明', '身分證', '申請書', '應備', '所需', '必備', '準備'])
        has_qualifications = any(kw in combined_content for kw in ['資格', '條件', '符合', '標準', '門檻', '適用', '對象'])
        has_amount = any(kw in combined_content for kw in ['金額', '費用', '價格', '收費', '補助', '元', '津貼', '定價', '方案'])
        has_process = any(kw in combined_content for kw in ['流程', '步驟', '如何申請', '如何購買', '退換貨', '維修', '保固', '退費'])
        has_booking = any(kw in combined_content for kw in ['營業時間', '門市', '分店', '預約', '掛號', '訂位', '地址', '位置'])

        if has_qualifications and len(buttons) < 3:
            buttons.append({"label": "✅ 適用條件", "text": f"{query_text[:10]} 適用條件"})
        if has_amount and len(buttons) < 3:
            buttons.append({"label": "💰 費用說明", "text": f"{query_text[:10]} 費用說明"})
        if has_process and len(buttons) < 3:
            buttons.append({"label": "🔄 服務流程", "text": f"{query_text[:10]} 服務流程"})
        if has_booking and len(buttons) < 3:
            buttons.append({"label": "📍 營業與預約", "text": f"{query_text[:10]} 營業與預約"})
        if has_docs and len(buttons) < 3:
            buttons.append({"label": "📄 準備資料", "text": f"{query_text[:10]} 準備資料"})

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
        # 解析 query string 格式 (例如 action=rag_query&text=服務介紹)
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
    company = await run_in_threadpool(get_company, company_slug)
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
