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
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:embedContent?key={GEMINI_KEY}"
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json()['embedding']['values']
        else:
            print(f"Gemini API Error: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"Request failed: {e}")
    return None

def chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
    """將長文本切塊，並保持邊緣重疊以避免語意斷層"""
    chunks = []
    start = 0
    text_len = len(text)
    
    if text_len <= chunk_size:
        return [text]
        
    while start < text_len:
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
        if start >= text_len - overlap:
            break
            
    return chunks

def main():
    chunk_size = 400
    overlap = 50

    print("--- Phase 1: Checking for active long articles to chunk ---")
    # 尋找 is_active = True 且不是 derived-chunk 的所有原始文章
    # 在 Supabase python query 中，對於 array 的篩選，我們可以在 python 端過濾，或使用 .not_
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
            
            # 1. 刪除之前由此 row 產生的舊 chunks
            # 在 Postgres text[] 中，我們可以使用 contains 來找包含特定 tag 的
            parent_tag = f"parent:{row_id}"
            try:
                # 找出 tags 包含 parent_tag 的並刪除
                # 我們可以用 .contains('tags', [parent_tag]) 來篩選
                old_chunks = supabase.table('knowledge_base').select('id').eq('company_id', company_id).contains('tags', [parent_tag]).execute()
                old_chunk_ids = [oc['id'] for oc in old_chunks.data or []]
                if old_chunk_ids:
                    print(f"  Deleting {len(old_chunk_ids)} old chunks...")
                    supabase.table('knowledge_base').delete().in_('id', old_chunk_ids).execute()
            except Exception as delete_err:
                print(f"  Warning deleting old chunks: {delete_err}")
                
            # 2. 開始切塊
            chunks = chunk_text(content, chunk_size, overlap)
            print(f"  Split into {len(chunks)} chunks.")
            
            # 3. 寫入新 chunks
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
                
            # 4. 將原本的長文件 row 標記為 is_active = False (不參與檢索)
            supabase.table('knowledge_base').update({'is_active': False}).eq('id', row_id).execute()
            chunked_count += 1

    print(f"Successfully processed and split {chunked_count} long documents.")

    print("\n--- Phase 2: Generating embeddings for active missing entries ---")
    # 尋找 is_active = True 且 embedding 尚未計算 (或維度不為 768) 的 rows
    res = supabase.table('knowledge_base').select('id, title, content, embedding').eq('is_active', True).execute()
    active_rows = res.data or []
    
    sync_count = 0
    for r in active_rows:
        row_id = r['id']
        title = r['title']
        content = r['content']
        current_embedding = r.get('embedding')
        
        # 如果已經有合法的 768 維 embedding，略過
        if current_embedding and len(current_embedding) == 768:
            continue
            
        print(f"Generating embedding for: '{title}'...")
        text_to_embed = f"標題：{title}\n內容：{content}"
        embedding = get_embedding(text_to_embed)
        time.sleep(0.8)  # Rate limiting safety
        
        if embedding:
            update_res = supabase.table('knowledge_base').update({'embedding': embedding}).eq('id', row_id).execute()
            if update_res.data:
                print(f"  Successfully updated embedding for '{title}'.")
                sync_count += 1
            else:
                print(f"  Failed to update DB for '{title}'.")
        else:
            print(f"  Failed to generate embedding for '{title}'.")
            
    print(f"\nDone! Generated {sync_count} new embeddings.")

if __name__ == '__main__':
    main()
