import os
import sys
import asyncio
import logging
from dotenv import load_dotenv
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("diagnostics")

async def main():
    parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env_path = os.path.join(parent_dir, '.env')
    load_dotenv(env_path)
    
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
    
    supabase = create_client(supabase_url, supabase_key)
    
    # Query companies
    companies_res = supabase.table('companies').select('*').execute()
    logger.info(f"Companies: {companies_res.data}")
    
    if companies_res.data:
        company_id = companies_res.data[0]['id']
        
        # Query assets
        assets_res = supabase.table('company_assets').select('*').eq('company_id', company_id).execute()
        logger.info(f"Company Assets Count: {len(assets_res.data)}")
        for a in assets_res.data:
            logger.info(f"Asset: name={a['name']}, url={a['url']}, type={a['action_type']}")
            
        # Query semantic cache count
        cache_res = supabase.table('semantic_cache').select('id', count='exact').execute()
        logger.info(f"Semantic Cache Count: {cache_res.count if hasattr(cache_res, 'count') else 'N/A'}")
        
        # Query chat history count
        history_res = supabase.table('chat_history').select('id', count='exact').eq('company_id', company_id).execute()
        logger.info(f"Chat History Count for company: {history_res.count if hasattr(history_res, 'count') else 'N/A'}")

if __name__ == '__main__':
    asyncio.run(main())
