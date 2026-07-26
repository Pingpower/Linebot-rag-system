import os
import sys
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Error: SUPABASE_URL or SUPABASE_SERVICE_KEY not found in environment.")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def clear_all_embeddings():
    print("Clearing old Gemini embeddings from knowledge_base...")
    res1 = supabase.table('knowledge_base').update({'embedding': None}).neq('id', '00000000-0000-0000-0000-000000000000').execute()
    print(f"Cleared knowledge_base embeddings.")
    
    print("Clearing old Gemini embeddings from semantic_cache...")
    res2 = supabase.table('semantic_cache').update({'embedding': None}).neq('id', '00000000-0000-0000-0000-000000000000').execute()
    print(f"Cleared semantic_cache embeddings.")

if __name__ == '__main__':
    clear_all_embeddings()
