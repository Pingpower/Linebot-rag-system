import os
import re
import json
import logging
import httpx
from starlette.concurrency import run_in_threadpool
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
logger = logging.getLogger(__name__)

# 全域持久 HTTP 客戶端 (連線池復用，避免每次建連)
_http_client: httpx.AsyncClient | None = None

def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10.0)
    return _http_client

# 初始化 Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')
sb_client: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    sb_client = create_client(SUPABASE_URL, SUPABASE_KEY)

def normalize_query(text: str) -> str:
    """
    對輸入的查詢進行標準化與降噪，去除常見開頭贅字、結尾語氣詞與無效標點符號。
    """
    if not text:
        return ""
    # 轉小寫，去前後空格
    text = text.strip().lower()
    # 移除開頭常見的贅詞
    prefix_pattern = r"^(請問|幫我查一下|幫我查|請告訴我|請幫我查詢|我想問|我想了解|有沒有|關於|有關於|想問一下)\s*"
    text = re.sub(prefix_pattern, "", text)
    # 移除結尾的虛詞或純語氣詞（保留「資料」、「資訊」等實詞）
    suffix_pattern = r"\s*(謝謝|感恩|拜託|呢|嗎|呀|啦|了|吧|捏)$"
    text = re.sub(suffix_pattern, "", text)
    # 移除常見的標點符號與多餘空白（保留數字、連字號等語意關鍵字）
    text = re.sub(r"[，。！？、~～`@#$^&\|\\;\"']+", "", text)
    text = re.sub(r"\s+", "", text)
    return text

async def get_embedding(text: str) -> list[float] | None:
    """取得 768 維度的 Gemini embedding (非同步)"""
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        logger.error("Semantic Cache: GEMINI_API_KEY not found in environment.")
        return None
    url = "https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:embedContent"
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768
    }
    headers = {"x-goog-api-key": gemini_key}
    try:
        client = get_http_client()
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code == 200:
            return r.json()['embedding']['values']
        else:
            logger.error(f"Semantic Cache: Gemini API Error: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Semantic Cache: get_embedding failed: {e}")
    return None

async def check_cache(company_id: str, query_text: str, threshold: float = 0.92, bypass_semantic: bool = False, query_embedding: list[float] | None = None) -> tuple[str | None, list[float] | None]:
    """
    檢查是否有高相似度的語意快取答案 (使用 Supabase pgvector) (非同步)
    支援精確比對 (Exact Match) 與降噪比對，若精確比對不中則降級為語意向量比對。
    回傳: (cached_reply_text_or_None, q_emb_or_None)
    """
    if not sb_client:
        return None, query_embedding

    # 助檢函式：自癒壞資料 (壞標記檢測)
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

    # 1. 雙層精確比對 (Exact Match)
    try:
        # 1.1 嘗試直接比對原始輸入 (例如 LINE 選單點擊)
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
                logger.info(f"Semantic Cache EXACT HIT (Raw) for '{query_text}' (ID: {matched['id']})")
                return valid_reply, query_embedding
            
        # 1.2 嘗試比對降噪標準化後的字串
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
                    logger.info(f"Semantic Cache EXACT HIT (Normalized) for '{query_text}' -> '{norm_query}' (ID: {matched['id']})")
                    return valid_reply, query_embedding
                
    except Exception as e_exact:
        logger.error(f"Semantic Cache exact match check failed: {e_exact}")

    # 如果啟用 bypass_semantic，跳過後續的向量計算與比對 (多用於短句)
    if bypass_semantic:
        logger.info(f"Semantic Cache bypass vector search for short query: '{query_text}'")
        return None, query_embedding

    # 2. 語意向量比對 (Cosine Similarity)
    norm_query = normalize_query(query_text)
    q_emb = query_embedding
    if not q_emb:
        q_emb = await get_embedding(norm_query or query_text)
    if not q_emb or len(q_emb) != 768:
        return None, None
        
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
            score = matched['similarity']
            matched_id = matched['id']
            
            logger.info(f"Semantic Cache HIT for '{query_text}' (normalized: '{norm_query}'): Score = {score:.4f} (ID: {matched_id})")
            valid_reply = await _verify_and_return(matched)
            return valid_reply, q_emb
    except Exception as e:
        logger.error(f"Semantic Cache search failed in Supabase: {e}")
            
    return None, q_emb

async def add_to_cache(company_id: str, query_text: str, reply_data: str, query_embedding: list[float] | None = None) -> bool:
    """將新的問答對象新增到 Supabase 語意快取中 (非同步)"""
    if not sb_client:
        return False
        
    norm_query = normalize_query(query_text)
    q_emb = query_embedding
    if not q_emb:
        q_emb = await get_embedding(norm_query or query_text)
    if not q_emb or len(q_emb) != 768:
        return False
        
    try:
        from starlette.concurrency import run_in_threadpool
        def _do_insert():
            return sb_client.table('semantic_cache').insert({
                'company_id': company_id,
                'query_text': query_text,  # 存入原始文字供未來精確比對
                'reply_data': reply_data,
                'embedding': q_emb,        # 使用降噪後的向量
                'is_active': True
            }).execute()
        await run_in_threadpool(_do_insert)
        logger.info(f"Successfully added query '{query_text}' (normalized: '{norm_query}') to Semantic Cache in Supabase.")
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
    使用批次 UPDATE 提升效能 (非同步)。
    """
    if not sb_client or not text or not text.strip():
        return 0
        
    norm_query = normalize_query(text)
    q_emb = await get_embedding(norm_query or text)
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
        logger.info(f"Batch invalidated {invalidated_count} Semantic Cache entries for company {company_id}.")
        return invalidated_count
    except Exception as e:
        logger.error(f"Supabase search for invalidation failed: {e}")
        return 0

