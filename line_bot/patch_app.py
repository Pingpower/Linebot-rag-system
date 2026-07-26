import re

def patch_app():
    with open('app.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. 注入啟動事件與模型預先載入，確保 FastAPI 啟動即 background 載入
    if 'from embedding_model import EmbeddingModelSingleton' not in content:
        startup_code = '''app = FastAPI(title="LM Bot API")

@app.on_event("startup")
async def startup_event():
    try:
        from embedding_model import EmbeddingModelSingleton
        EmbeddingModelSingleton.preload()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to preload EmbeddingModelSingleton: {e}")'''
        content = content.replace('app = FastAPI(title="LM Bot API")', startup_code)

    # 2. 替換背景任務中的 subprocess (第一處) 為直接呼叫
    content = re.sub(
        r'script_path = os\.path\.join\(os\.path\.dirname\(os\.path\.abspath\(__file__\)\), "sync_embeddings\.py"\)\s+subprocess\.run\(\[sys\.executable, script_path\], stdout=subprocess\.DEVNULL, stderr=subprocess\.DEVNULL\)',
        'import sync_embeddings\\n            sync_embeddings.main()',
        content
    )

    # 3. 替換背景任務中的 subprocess (第二處) 為直接呼叫
    content = re.sub(
        r'script_path = os\.path\.join\(os\.path\.dirname\(os\.path\.abspath\(__file__\)\), "sync_embeddings\.py"\)\s+res = subprocess\.run\(\[sys\.executable, script_path\], capture_output=True, text=True\)\s+if res\.returncode != 0:\s+logger\.error\(f"sync_embeddings\.py failed with exit code \\{res\.returncode\\}\. Stderr: \\{res\.stderr\\}"\)\s+else:\s+logger\.info\(f"Knowledge update background sync completed successfully\. Output: \\{res\.stdout\.strip\(\\)\\}"\)',
        'import sync_embeddings\\n                sync_embeddings.main()\\n                logger.info("Knowledge update background sync completed successfully.")',
        content
    )

    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == "__main__":
    patch_app()
