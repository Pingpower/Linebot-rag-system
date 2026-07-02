import os
import time
import json
import logging
import threading
import numpy as np
import requests as req_lib
from dotenv import load_dotenv
from supabase import create_client, Client
import turbovec

load_dotenv()
logger = logging.getLogger(__name__)

# 初始化 Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')
sb_client: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    sb_client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 索引檔案路徑設定
INDEX_DIR = os.path.expanduser("~/文件/models")
INDEX_PATH = os.path.join(INDEX_DIR, "semantic_cache.tvim")
os.makedirs(INDEX_DIR, exist_ok=True)

# 執行緒鎖，確保 Turbovec 寫入與讀取安全
cache_lock = threading.Lock()
_index = None

def get_embedding(text: str) -> list[float] | None:
    """取得 3072 維度的 Gemini embedding"""
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        logger.error("Semantic Cache: GEMINI_API_KEY not found in environment.")
        return None
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:embedContent?key={gemini_key}"
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": text}]}
    }
    try:
        r = req_lib.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()['embedding']['values']
        else:
            logger.error(f"Semantic Cache: Gemini API Error: {r.status_code} - {r.text}")
    except Exception as e:
        logger.error(f"Semantic Cache: get_embedding failed: {e}")
    return None

def _get_or_create_index() -> turbovec.IdMapIndex:
    """延遲載入或建立 Turbovec 索引"""
    global _index
    if _index is not None:
        return _index
        
    with cache_lock:
        # 雙重檢查鎖
        if _index is not None:
            return _index
            
        if os.path.exists(INDEX_PATH):
            try:
                logger.info(f"Loading existing Turbovec index from {INDEX_PATH}")
                _index = turbovec.IdMapIndex.load(INDEX_PATH)
            except Exception as e:
                logger.error(f"Failed to load Turbovec index: {e}, creating a new one.")
                _index = turbovec.IdMapIndex(dim=3072, bit_width=4)
                _index.write(INDEX_PATH)
        else:
            logger.info(f"No Turbovec index found, initializing new index at {INDEX_PATH}")
            _index = turbovec.IdMapIndex(dim=3072, bit_width=4)
            _index.write(INDEX_PATH)
            
        return _index

def check_cache(company_id: str, query_text: str, threshold: float = 0.92) -> str | None:
    """
    檢查是否有高相似度的語意快取答案
    
    threshold: 相似度門檻（餘弦相似度/點積），預設 0.92 左右
    """
    if not sb_client:
        return None
        
    q_emb = get_embedding(query_text)
    if not q_emb or len(q_emb) != 3072:
        return None
        
    index = _get_or_create_index()
    if len(index) == 0:
        return None
        
    # 將向量轉換為 NumPy Array 以便 turbovec 處理
    query_vec = np.array([q_emb], dtype=np.float32)
    
    with cache_lock:
        try:
            scores, ids = index.search(query_vec, 1)
        except Exception as e:
            logger.error(f"Turbovec search failed: {e}")
            return None
            
    if len(scores) == 0 or len(scores[0]) == 0:
        return None
        
    score = scores[0][0]
    matched_id = int(ids[0][0])
    
    logger.info(f"Semantic Cache check for '{query_text}': Max Score = {score:.4f} (ID: {matched_id})")
    
    # 若相似度高於設定的門檻，且不是負數（理論上點積範圍在 -1 到 1）
    if score >= threshold:
        try:
            # 向 Supabase 查詢這筆快取對應的回覆
            res = (
                sb_client.table('semantic_cache')
                .select('reply_data')
                .eq('id', matched_id)
                .eq('company_id', company_id)
                .eq('is_active', True)
                .execute()
            )
            if res.data and len(res.data) > 0:
                reply_data = res.data[0]['reply_data']
                # 防禦：若發現已存的回覆中含有壞掉的 code block 標記，將其失效並略過
                if ",,," in reply_data or ", , ," in reply_data:
                    logger.warning(f"Semantic Cache contains faulty markers (,,,), invalidating ID {matched_id}")
                    try:
                        sb_client.table('semantic_cache').update({'is_active': False}).eq('id', matched_id).execute()
                    except Exception as e_upd:
                        logger.error(f"Failed to invalidate faulty cache ID {matched_id}: {e_upd}")
                    return None
                logger.info(f"Semantic Cache HIT! (Similarity: {score:.4f})")
                return reply_data
        except Exception as e:
            logger.error(f"Failed to query semantic_cache table from Supabase: {e}")
            
    return None

def add_to_cache(company_id: str, query_text: str, reply_data: str) -> bool:
    """將新的問答對象新增到語意快取與 Supabase 中"""
    if not sb_client:
        return False
        
    q_emb = get_embedding(query_text)
    if not q_emb or len(q_emb) != 3072:
        return False
        
    index = _get_or_create_index()
    
    # 決定一個唯一 ID (bigint)
    # 我們向 Supabase 取得當前最大的 id，並加 1
    new_id = 1
    try:
        res = (
            sb_client.table('semantic_cache')
            .select('id')
            .order('id', desc=True)
            .limit(1)
            .execute()
        )
        if res.data and len(res.data) > 0:
            new_id = int(res.data[0]['id']) + 1
    except Exception as e:
        logger.warning(f"Failed to fetch max ID from Supabase, fallback to timestamp-based ID: {e}")
        # 若失敗則用隨機時間戳作為 fallback ID (大整數)
        new_id = int(time.time() * 1000) % 9223372036854775807
        
    # 將資料存入 Supabase
    try:
        sb_client.table('semantic_cache').insert({
            'id': new_id,
            'company_id': company_id,
            'query_text': query_text,
            'reply_data': reply_data,
            'is_active': True
        }).execute()
    except Exception as e:
        logger.error(f"Failed to insert cache metadata into Supabase: {e}")
        return False
        
    # 將向量寫入 Turbovec
    vec_data = np.array([q_emb], dtype=np.float32)
    ids_data = np.array([new_id], dtype=np.uint64)
    
    with cache_lock:
        try:
            index.add_with_ids(vec_data, ids_data)
            index.write(INDEX_PATH)
            logger.info(f"Successfully added query '{query_text}' to Semantic Cache (ID: {new_id})")
            return True
        except Exception as e:
            logger.error(f"Failed to add vector to Turbovec index: {e}")
            # 回滾 Supabase
            try:
                sb_client.table('semantic_cache').delete().eq('id', new_id).execute()
            except:
                pass
            return False

def remove_from_cache(cache_id: int) -> bool:
    """從快取庫中移除特定 ID"""
    if not sb_client:
        return False
        
    index = _get_or_create_index()
    
    try:
        # 從 Supabase 刪除
        sb_client.table('semantic_cache').delete().eq('id', cache_id).execute()
    except Exception as e:
        logger.error(f"Failed to delete cache {cache_id} from Supabase: {e}")
        return False
        
    with cache_lock:
        try:
            index.remove(cache_id)
            index.write(INDEX_PATH)
            logger.info(f"Successfully removed ID {cache_id} from Semantic Cache index.")
            return True
        except Exception as e:
            logger.error(f"Failed to remove vector {cache_id} from Turbovec: {e}")
            return False


def invalidate_semantic_cache_by_text(company_id: str, text: str, threshold: float = 0.85) -> int:
    """
    根據給定文字，搜尋語意相似的快取項目並將其設為無效（is_active = False）。
    
    回傳被標記無效的快取數量。
    """
    if not sb_client or not text or not text.strip():
        return 0
        
    q_emb = get_embedding(text)
    if not q_emb or len(q_emb) != 3072:
        return 0
        
    index = _get_or_create_index()
    if len(index) == 0:
        return 0
        
    query_vec = np.array([q_emb], dtype=np.float32)
    
    with cache_lock:
        try:
            # 搜尋前 10 筆相似的快取
            scores, ids = index.search(query_vec, min(10, len(index)))
        except Exception as e:
            logger.error(f"Turbovec search for invalidation failed: {e}")
            return 0
            
    if len(scores) == 0 or len(scores[0]) == 0:
        return 0
        
    invalidated_count = 0
    for score, matched_id_val in zip(scores[0], ids[0]):
        matched_id = int(matched_id_val)
        if score >= threshold:
            try:
                # 將快取在 Supabase 中設為失效
                res = (
                    sb_client.table('semantic_cache')
                    .update({'is_active': False})
                    .eq('id', matched_id)
                    .eq('company_id', company_id)
                    .execute()
                )
                if res.data:
                    logger.info(f"Invalidated Semantic Cache ID {matched_id} (Score: {score:.4f}) due to update in knowledge base.")
                    invalidated_count += 1
            except Exception as e:
                logger.error(f"Failed to invalidate cache ID {matched_id}: {e}")
                
    return invalidated_count

