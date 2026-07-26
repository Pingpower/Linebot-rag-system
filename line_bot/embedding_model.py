import asyncio
import logging
import threading
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)
EMBEDDING_DIM = 768

class EmbeddingModelSingleton:
    _instance = None
    _model = None
    _async_lock = asyncio.Lock()
    _sync_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EmbeddingModelSingleton, cls).__new__(cls)
        return cls._instance

    @classmethod
    async def get_model(cls):
        if cls._model is not None:
            return cls._model

        async with cls._async_lock:
            if cls._model is None:
                logger.info("Loading PyTorch embedding model...")
                cls._model = await asyncio.to_thread(
                    lambda: SentenceTransformer("moka-ai/m3e-base", device="cpu")
                )
                logger.info("PyTorch embedding model loaded.")
        return cls._model

    @classmethod
    async def get_embedding(cls, text: str) -> list[float] | None:
        if not text or not text.strip():
            return None
            
        model = await cls.get_model()
        embedding = await asyncio.to_thread(lambda: model.encode(text).tolist())
        return embedding

    @classmethod
    async def preload_async(cls):
        """用於 FastAPI 啟動時預先載入模型，確切等待載入完成"""
        await cls.get_model()

    @classmethod
    def get_embedding_sync(cls, text: str) -> list[float] | None:
        """提供給同步腳本 (如 sync_embeddings.py) 使用，具備雙重鎖防禦」"""
        if not text or not text.strip():
            return None
            
        if cls._model is None:
            with cls._sync_lock:
                if cls._model is None:
                    cls._model = SentenceTransformer("moka-ai/m3e-base", device="cpu")
        
        return cls._model.encode(text).tolist()
