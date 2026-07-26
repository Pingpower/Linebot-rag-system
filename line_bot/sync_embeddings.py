import os
import sys
import re
from dotenv import load_dotenv
from supabase import create_client, Client
from embedding_model import EmbeddingModelSingleton

load_dotenv()

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Error: SUPABASE_URL or SUPABASE_SERVICE_KEY not found in environment.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_embedding(text: str) -> list[float] | None:
    if not text or not text.strip():
        return None
    try:
        return EmbeddingModelSingleton.get_embedding_sync(text)
    except Exception as e:
        print(f"Request failed: {e}")
        return None

def smart_chunk(text: str, max_size: int = 400, overlap: int = 80) -> list[str]:
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
                
                while len(sub_current) > max_size:
                    chunks.append(sub_current[:max_size].strip())
                    sub_current = sub_current[max_size:]
                
                current = sub_current
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())

    return chunks

def main():
    chunk_size = 400
    overlap = 50

    print("--- Phase 1: Checking for active long articles to chunk ---")
    try:
        res = supabase.table('knowledge_base').select('id, company_id, title, content, tags').eq('is_active', True).execute()
        rows = res.data or []
    except Exception as e:
        print(f"Failed to query knowledge base: {e}")
        return
    
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
            except Exception as old_err:
                old_chunk_ids = []
                
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
            
            try:
                if new_rows:
                    supabase.table('knowledge_base').insert(new_rows).execute()
                if old_chunk_ids:
                    print(f"  Deleting {len(old_chunk_ids)} old chunks...")
                    supabase.table('knowledge_base').delete().in_('id', old_chunk_ids).execute()
                supabase.table('knowledge_base').update({'is_active': False}).eq('id', row_id).execute()
                chunked_count += 1
            except Exception as insert_err:
                print(f"  Error inserting chunks for {title}: {insert_err}")

    print(f"Successfully processed and split {chunked_count} long documents.")

    print("\n--- Phase 2: Generating embeddings for active missing entries ---")
    try:
        res = supabase.table('knowledge_base').select('id, title, content, embedding').eq('is_active', True).execute()
        active_rows = res.data or []
    except Exception as e:
        print(f"Failed to fetch active rows for embeddings: {e}")
        return
    
    rows_to_embed = []
    for r in active_rows:
        current_embedding = r.get('embedding')
        if not current_embedding or len(current_embedding) != 768:
            rows_to_embed.append(r)
            
    print(f"Found {len(rows_to_embed)} rows needing embeddings.")

    sync_count = 0
    # 移除 ThreadPoolExecutor, CPU bound 的 PyTorch 運算應避免線程池衝突，改為循序處理
    for r in rows_to_embed:
        row_id = r['id']
        title = r['title']
        content = r['content']
        text_to_embed = f"標題：{title}\n內容：{content}"
        
        emb = get_embedding(text_to_embed)
        if emb:
            try:
                res_upd = supabase.table('knowledge_base').update({'embedding': emb}).eq('id', row_id).execute()
                if res_upd.data:
                    print(f"  Successfully updated embedding for '{title}'.")
                    sync_count += 1
            except Exception as upd_e:
                print(f"  Failed to update DB for '{title}': {upd_e}")
        else:
            print(f"  Failed to process '{title}'.")

    print(f"\nDone! Generated {sync_count} new embeddings.")

if __name__ == '__main__':
    main()
