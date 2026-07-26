import os
import re
import json
import logging
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv
from supabase import create_client, Client
from embedding_model import EmbeddingModelSingleton

load_dotenv()
logger = logging.getLogger(__name__)

# 初始化 Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')
sb_client: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    sb_client = create_client(SUPABASE_URL, SUPABASE_KEY)

def normalize_query(text: str) -> str:
    if not text:
        return ""
    text = text.strip().lower()
    prefix_pattern = r"^(請問|幫我查一下|幫我查|請告訴我|請幫我查詢|我想問|我想了解|有沒有|關於|有關於|想問一下)\s*"
    text = re.sub(prefix_pattern, "", text)
    suffix_pattern = r"\s*(謝謝|感恩|拜託|呢|嗎|呀|啦|了|吧|捏)$"
    text = re.sub(suffix_pattern, "", text)
    text = re.sub(r"[，。！？、~～`@#$^&\|\\;\"']+", "", text)
    text = re.sub(r"\s+", "", text)
    return text

async def get_embedding(text: str) -> list[float] | None:
    """取得 768 維度的 embedding (改用本地模型，移除寫死的 Gemini API URL)"""
    if not text or not text.strip():
        return None
    try:
        return await EmbeddingModelSingleton.get_embedding(text)
    except Exception as e:
        logger.error(f"Semantic Cache: get_embedding failed: {e}")
        return None

async def check_cache(company_id: str, query_text: str, threshold: float = 0.92, bypass_semantic: bool = False, query_embedding: list[float] | None = None) -> tuple[str | None, list[float] | None]:
    if not sb_client:
        return None, query_embedding

    async def _verify_and_return(matched_item: dict) -> str | None:
        reply_data = matched_item.get('reply_data', '')
        matched_id = matched_item.get('id')
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

    try:
        def _do_exact_raw():
            return sb_client.table('semantic_cache') \
                .select('reply_data', 'id') \
                .eq('company_id', company_id) \
                .eq('is_active', True) \
                .eq('query_text', query_text) \
                .limit(1) \
                .execute()
                
        res_raw = await run_in_threadpool(_do_exact_raw)
        if res_raw.data and len(res_raw.data) > 0:
            matched = res_raw.data[0]
            valid_reply = await _verify_and_return(matched)
            if valid_reply:
                return valid_reply, query_embedding
            
        norm_query = normalize_query(query_text)
        if norm_query and norm_query != query_text:
            def _do_exact_norm():
                return sb_client.table('semantic_cache') \
                    .select('reply_data', 'id') \
                    .eq('company_id', company_id) \
                    .eq('is_active', True) \
                    .eq('query_text', norm_query) \
                    .limit(1) \
                    .execute()
                    
            res_norm = await run_in_threadpool(_do_exact_norm)
            if res_norm.data and len(res_norm.data) > 0:
                matched = res_norm.data[0]
                valid_reply = await _verify_and_return(matched)
                if valid_reply:
                    return valid_reply, query_embedding
                
    except Exception as e_exact:
        logger.error(f"Semantic Cache exact match check failed: {e_exact}")

    if bypass_semantic:
        return None, query_embedding

    norm_query = normalize_query(query_text)
    q_emb = query_embedding
    if not q_emb:
        q_emb = await get_embedding(norm_query or query_text)
    if not q_emb or len(q_emb) != 768:
        return None, None
        
    try:
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
            valid_reply = await _verify_and_return(matched)
            return valid_reply, q_emb
    except Exception as e:
        logger.error(f"Semantic Cache search failed in Supabase: {e}")
            
    return None, q_emb

async def add_to_cache(company_id: str, query_text: str, reply_data: str, query_embedding: list[float] | None = None) -> bool:
    if not sb_client:
        return False
        
    norm_query = normalize_query(query_text)
    q_emb = query_embedding
    if not q_emb:
        q_emb = await get_embedding(norm_query or query_text)
    if not q_emb or len(q_emb) != 768:
        return False
        
    try:
        def _do_insert():
            return sb_client.table('semantic_cache').insert({
                'company_id': company_id,
                'query_text': query_text,
                'reply_data': reply_data,
                'embedding': q_emb,
                'is_active': True
            }).execute()
        await run_in_threadpool(_do_insert)
        return True
    except Exception as e:
        logger.error(f"Semantic Cache add to cache failed: {e}")
        return False

async def remove_from_cache(cache_id: str) -> bool:
    if not sb_client:
        return False
        
    try:
        def _do_delete():
            return sb_client.table('semantic_cache').delete().eq('id', cache_id).execute()
        await run_in_threadpool(_do_delete)
        logger.info(f"Successfully removed ID {cache_id} from Semantic Cache.")
        return True
    except Exception as e:
        logger.error(f"Failed to delete cache {cache_id} from Supabase: {e}")
        return False

async def invalidate_semantic_cache_by_text(company_id: str, text: str, threshold: float = 0.85) -> int:
    if not sb_client or not text or not text.strip():
        return 0
        
    norm_query = normalize_query(text)
    q_emb = await get_embedding(norm_query or text)
    if not q_emb or len(q_emb) != 768:
        return 0
        
    try:
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
            
        matched_ids = [matched['id'] for matched in res.data if 'id' in matched]
        if not matched_ids:
            return 0

        def _do_batch_update():
            return sb_client.table('semantic_cache') \
                .update({'is_active': False}) \
                .in_('id', matched_ids) \
                .eq('company_id', company_id) \
                .execute()

        upd_res = await run_in_threadpool(_do_batch_update)
        invalidated_count = len(upd_res.data) if upd_res.data else len(matched_ids)
        return invalidated_count
    except Exception as e:
        logger.error(f"Supabase search for invalidation failed: {e}")
        return 0
