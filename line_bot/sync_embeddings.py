import os
import sys
import time
import requests
import re
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')
GEMINI_KEY = os.getenv('GEMINI_API_KEY', '')

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Error: SUPABASE_URL or SUPABASE_SERVICE_KEY not found in environment.")
    sys.exit(1)

if not GEMINI_KEY:
    print("Error: GEMINI_API_KEY not found in environment.")
    sys.exit(1)

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_embedding(text: str) -> list[float] | None:
    """Get 768-dimension embedding using Gemini's gemini-embedding-2 model"""
    url = "https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:embedContent"
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768
    }
    headers = {"x-goog-api-key": GEMINI_KEY}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json()['embedding']['values']
        else:
            print(f"Gemini API Error: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"Request failed: {e}")
    return None

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def smart_chunk(text: str, max_size: int = 400, overlap: int = 80) -> list[str]:
    """Split text at semantic boundaries (paragraphs > sentences > fallback cut)."""
    if len(text) <= max_size:
        return [text]

    chunks = []
    paragraphs = re.split(r'\n{2,}', text)
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_size:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current.strip())
            if len(para) > max_size:
                sentences = re.split(r'(?<=[。.！？\n])', para)
                sub_current = ""
                for sent in sentences:
                    if len(sub_current) + len(sent) <= max_size:
                        sub_current += sent
                    else:
                        if sub_current:
                            chunks.append(sub_current.strip())
                        sub_current = sent
                
                # 如果切完 sub_current 還是超大，硬切斷
                while len(sub_current) > max_size:
                    chunks.append(sub_current[:max_size].strip())
                    sub_current = sub_current[max_size:]
                
                current = sub_current
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())

    return chunks


class RateLimiter:
    """Token-bucket rate limiter to enforce max calls per second."""
    def __init__(self, max_per_second: int = 5):
        self.interval = 1.0 / max_per_second
        self.lock = threading.Lock()
        self.last_call = 0.0

    def wait(self):
        sleep_time = 0
        with self.lock:
            now = time.time()
            sleep_time = self.last_call + self.interval - now
            if sleep_time > 0:
                self.last_call += self.interval
            else:
                self.last_call = now
                
        if sleep_time > 0:
            time.sleep(sleep_time)


limiter = RateLimiter(max_per_second=5)


def main():
    chunk_size = 400
    overlap = 50

    print("--- Phase 1: Checking for active long articles to chunk ---")
    res = supabase.table('knowledge_base').select('id, company_id, title, content, tags').eq('is_active', True).execute()
    rows = res.data or []
    
    original_docs = []
    for r in rows:
        tags = r.get('tags') or []
        if 'derived-chunk' not in tags:
            original_docs.append(r)
            
    print(f"Found {len(original_docs)} original active documents.")
    
    chunked_count = 0
    for doc in original_docs:
        row_id = doc['id']
        company_id = doc['company_id']
        title = doc['title']
        content = doc['content']
        tags = doc.get('tags') or []
        
        if len(content) > chunk_size:
            print(f"Chunking long document: '{title}' ({len(content)} chars)")
            parent_tag = f"parent:{row_id}"
            try:
                old_chunks = supabase.table('knowledge_base').select('id').eq('company_id', company_id).contains('tags', [parent_tag]).execute()
                old_chunk_ids = [oc['id'] for oc in old_chunks.data or []]
                if old_chunk_ids:
                    print(f"  Deleting {len(old_chunk_ids)} old chunks...")
                    supabase.table('knowledge_base').delete().in_('id', old_chunk_ids).execute()
            except Exception as delete_err:
                print(f"  Warning deleting old chunks: {delete_err}")
                
            chunks = smart_chunk(content, chunk_size, overlap)
            print(f"  Split into {len(chunks)} smart chunks.")
            
            new_rows = []
            for idx, chunk_content in enumerate(chunks):
                new_tags = tags.copy()
                if 'derived-chunk' not in new_tags:
                    new_tags.append('derived-chunk')
                new_tags.append(parent_tag)
                
                new_rows.append({
                    'company_id': company_id,
                    'title': f"{title} (第 {idx+1} 部分)",
                    'content': chunk_content,
                    'tags': new_tags,
                    'is_active': True,
                    'embedding': None
                })
            
            if new_rows:
                supabase.table('knowledge_base').insert(new_rows).execute()
                
            supabase.table('knowledge_base').update({'is_active': False}).eq('id', row_id).execute()
            chunked_count += 1

    print(f"Successfully processed and split {chunked_count} long documents.")

    print("\n--- Phase 2: Generating embeddings for active missing entries ---")
    res = supabase.table('knowledge_base').select('id, title, content, embedding').eq('is_active', True).execute()
    active_rows = res.data or []
    
    rows_to_embed = []
    for r in active_rows:
        current_embedding = r.get('embedding')
        if not current_embedding or len(current_embedding) != 768:
            rows_to_embed.append(r)
            
    print(f"Found {len(rows_to_embed)} rows needing embeddings.")

    def _embed_and_update(r):
        limiter.wait()
        row_id = r['id']
        title = r['title']
        content = r['content']
        text_to_embed = f"標題：{title}\n內容：{content}"
        emb = get_embedding(text_to_embed)
        if emb:
            res_upd = supabase.table('knowledge_base').update({'embedding': emb}).eq('id', row_id).execute()
            if res_upd.data:
                print(f"  Successfully updated embedding for '{title}'.")
                return True
        print(f"  Failed to process '{title}'.")
        return False

    sync_count = 0
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_embed_and_update, r) for r in rows_to_embed]
        for future in as_completed(futures):
            if future.result():
                sync_count += 1
            
    print(f"\nDone! Generated {sync_count} new embeddings.")

if __name__ == '__main__':
    main()
