import os
import sys
import requests
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
    """Get 3072-dimension embedding using Gemini's gemini-embedding-2 model"""
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:embedContent?key={GEMINI_KEY}"
    payload = {
        "model": "models/gemini-embedding-2",
        "content": {"parts": [{"text": text}]}
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

def main():
    print("Fetching active knowledge base entries...")
    # Fetch all active rows
    res = supabase.table('knowledge_base').select('id, title, content, embedding').eq('is_active', True).execute()
    rows = res.data or []
    
    if not rows:
        print("No active knowledge base entries found.")
        return

    print(f"Found {len(rows)} entries. Syncing embeddings...")
    count = 0
    for row in rows:
        row_id = row['id']
        title = row.get('title', '')
        content = row.get('content', '')
        current_embedding = row.get('embedding')
        
        # Check if already has valid embedding (length 3072)
        if current_embedding and len(current_embedding) == 3072:
            print(f"Skipping row {row_id} ('{title}'): Already has a valid embedding.")
            continue
            
        print(f"Generating embedding for '{title}'...")
        text_to_embed = f"標題：{title}\n內容：{content}"
        embedding = get_embedding(text_to_embed)
        
        if embedding:
            # Update row in supabase
            update_res = supabase.table('knowledge_base').update({'embedding': embedding}).eq('id', row_id).execute()
            if update_res.data:
                print(f"Successfully updated embedding for row {row_id}.")
                count += 1
            else:
                print(f"Failed to update database for row {row_id}.")
        else:
            print(f"Failed to generate embedding for row {row_id}.")
            
    print(f"\nDone! Successfully updated {count} embeddings.")

if __name__ == '__main__':
    main()
