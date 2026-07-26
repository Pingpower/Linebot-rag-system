import pytest
import asyncio
from embedding_model import EmbeddingModelSingleton

@pytest.mark.asyncio
async def test_singleton_concurrency():
    """Test that multiple concurrent requests to get_model return the same instance and do not load multiple times."""
    
    # 為了測試，重置單例狀態
    EmbeddingModelSingleton._instance = None
    EmbeddingModelSingleton._model = None
    EmbeddingModelSingleton._ready_event = asyncio.Event()

    async def fetch_model():
        return await EmbeddingModelSingleton.get_model()

    # 模擬 10 個併發請求同時觸發加載模型
    tasks = [asyncio.create_task(fetch_model()) for _ in range(10)]
    models = await asyncio.gather(*tasks)

    # 驗證所有返回的模型實例皆為同一物件 (確保只有一個加載線程成功執行)
    first_model = models[0]
    for model in models[1:]:
        assert model is first_model

@pytest.mark.asyncio
async def test_get_embedding_empty_string():
    """Test get_embedding with empty or whitespace string."""
    assert await EmbeddingModelSingleton.get_embedding("") is None
    assert await EmbeddingModelSingleton.get_embedding("   ") is None
