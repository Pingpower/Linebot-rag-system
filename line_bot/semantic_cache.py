import os
import json
import logging
import httpx
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
logger = logging.getLogger(__name__)

# 初始化 Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')
sb_client: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    sb_client = create_client(SUPABASE_URL, SUPABASE_KEY)

async def get_embedding(text: str) -> list[float] | None:
    """取得 768 維度的 Gemini embedding (非同步)"""
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        logger.error("Semantic Cache: GEMINI_API_KEY not found in environment.")
        return None
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:embedContent?key={gemini_key}"
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768
    }
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, timeout=10.0)
        if r.status_code == 200:
            return r.json()['embedding']['values']
        else:
            logger.error(f"Semantic Cache: Gemini API Error: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Semantic Cache: get_embedding failed: {e}")
    return None

async def check_cache(company_id: str, query_text: str, threshold: float = 0.92) -> str | None:
    """
    檢查是否有高相似度的語意快取答案 (使用 Supabase pgvector) (非同步)
    """
    if not sb_client:
        return None
        
    q_emb = await get_embedding(query_text)
    if not q_emb or len(q_emb) != 768:
        return None
        
    try:
        from starlette.concurrency import run_in_threadpool
        
        def _do_rpc():
            return sb_client.rpc('match_semantic_cache', {
                'query_embedding': q_emb,
                'match_threshold': threshold,
                'match_count': 1,
                'company_filter': company_id
            }).execute()
            
        res = await run_in_threadpool(_do_rpc)
        
        if res.data and len(res.data) > 0:
            matched = res.data[0]
            reply_data = matched['reply_data']
            score = matched['similarity']
            matched_id = matched['id']
            
            logger.info(f"Semantic Cache HIT for '{query_text}': Score = {score:.4f} (ID: {matched_id})")
            
            if ",,," in reply_data or ", , ," in reply_data:
                logger.warning(f"Semantic Cache contains faulty markers (,,,), invalidating ID {matched_id}")
                try:
                    def _do_update():
                        return sb_client.table('semantic_cache').update({'is_active': False}).eq('id', matched_id).execute()
                    await run_in_threadpool(_do_update)
                except Exception as e_upd:
                    logger.error(f"Failed to invalidate faulty cache ID {matched_id}: {e_upd}")
                return None
                
            return reply_data
    except Exception as e:
        logger.error(f"Semantic Cache search failed in Supabase: {e}")
            
    return None

async def add_to_cache(company_id: str, query_text: str, reply_data: str) -> bool:
    """將新的問答對象新增到 Supabase 語意快取中 (非同步)"""
    if not sb_client:
        return False
        
    q_emb = await get_embedding(query_text)
    if not q_emb or len(q_emb) != 768:
        return False
        
    try:
        from starlette.concurrency import run_in_threadpool
        def _do_insert():
            return sb_client.table('semantic_cache').insert({
                'company_id': company_id,
                'query_text': query_text,
                'reply_data': reply_data,
                'embedding': q_emb,
                'is_active': True
            }).execute()
        await run_in_threadpool(_do_insert)
        logger.info(f"Successfully added query '{query_text}' to Semantic Cache in Supabase.")
        return True
    except Exception as e:
        logger.error(f"Failed to insert cache metadata into Supabase: {e}")
        return False

async def remove_from_cache(cache_id: str) -> bool:
    """從快取庫中移除特定 ID (非同步)"""
    if not sb_client:
        return False
        
    try:
        from starlette.concurrency import run_in_threadpool
        def _do_delete():
            return sb_client.table('semantic_cache').delete().eq('id', cache_id).execute()
        await run_in_threadpool(_do_delete)
        logger.info(f"Successfully removed ID {cache_id} from Semantic Cache.")
        return True
    except Exception as e:
        logger.error(f"Failed to delete cache {cache_id} from Supabase: {e}")
        return False

async def invalidate_semantic_cache_by_text(company_id: str, text: str, threshold: float = 0.85) -> int:
    """
    根據給定文字，搜尋語意相似的快取項目並將其設為無效（is_active = False）。
    回傳被標記無效的快取數量 (非同步)。
    """
    if not sb_client or not text or not text.strip():
        return 0
        
    q_emb = await get_embedding(text)
    if not q_emb or len(q_emb) != 768:
        return 0
        
    try:
        from starlette.concurrency import run_in_threadpool
        def _do_rpc():
            return sb_client.rpc('match_semantic_cache', {
                'query_embedding': q_emb,
                'match_threshold': threshold,
                'match_count': 10,
                'company_filter': company_id
            }).execute()
        res = await run_in_threadpool(_do_rpc)
        
        if not res.data:
            return 0
            
        invalidated_count = 0
        for matched in res.data:
            matched_id = matched['id']
            score = matched['similarity']
            try:
                def _do_update_invalid():
                    return sb_client.table('semantic_cache').update({'is_active': False}).eq('id', matched_id).eq('company_id', company_id).execute()
                upd_res = await run_in_threadpool(_do_update_invalid)
                if upd_res.data:
                    logger.info(f"Invalidated Semantic Cache ID {matched_id} (Score: {score:.4f}) due to update in knowledge base.")
                    invalidated_count += 1
            except Exception as e:
                logger.error(f"Failed to invalidate cache ID {matched_id}: {e}")
                
        return invalidated_count
    except Exception as e:
        logger.error(f"Supabase search for invalidation failed: {e}")
        return 0
