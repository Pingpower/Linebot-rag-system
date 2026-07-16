# ADR-004: Semantic Cache Optimization (Exact Match & Normalization)

## Status
Accepted (2026-07-17)

## Context
The LINE Bot's RAG system heavily relied on Semantic Cache (Cosine Similarity via Gemini Embedding) to speed up responses and save API costs. However, several issues were observed:
1. **Short queries (buttons/options)**: Queries under 12 characters bypassed the semantic cache entirely to prevent false positives from generic words. This meant users clicking buttons (e.g. "生育補助") always triggered the full RAG pipeline, which is slow and costly.
2. **Noise sensitivity**: Queries with identical intents but different punctuation or filler words (e.g., "請問辦公時間？" vs "辦公時間") would sometimes yield low similarity scores or miss the cache.
3. **Latency**: Calling the Gemini Embedding API for every incoming message (even exact matches) added 0.5s - 1.5s of latency before we could even query the database.

## Decision
We redesigned the caching strategy in `semantic_cache.py` by introducing a **multi-layered cache pipeline**:
1. **Raw Exact Match**: Directly query the Supabase `semantic_cache` table for an exact string match. (Latency ~50ms, no API calls).
2. **Normalized Exact Match**: Apply a regex-based `normalize_query()` function to strip filler words ("請問", "幫我查"), punctuation, and trailing particles ("呢", "嗎"). Then perform an exact match.
3. **Bypass Check**: If both exact matches fail, check if the query is shorter than 12 characters. If so, bypass the semantic vector search.
4. **Semantic Vector Search**: If >= 12 characters, generate the Gemini embedding of the normalized query and perform a vector search (Cosine Similarity).

## Consequences
### Positive
- **Instant responses for buttons**: LINE Bot suggested buttons (usually short) now hit the Exact Match layer instantly.
- **Cost Reduction**: Exact matches no longer require calling the Gemini Embedding API.
- **Robustness**: The normalization layer increases the cache hit rate for casual queries.

### Negative
- **Database Load**: Up to 3 database queries (Raw, Normalized, Vector) are executed for a cache miss, but since the exact matches are indexed B-tree queries, the overhead is negligible.
