-- ============================================
-- 多租戶 LINE Bot 平台 資料表 Schema (Optimized)
-- 請到 Supabase Dashboard > SQL Editor 執行此檔案
-- ============================================

-- Enable the pgvector extension to support vector data types
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. 公司設定表
CREATE TABLE IF NOT EXISTS companies (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                  text UNIQUE NOT NULL,           -- URL identifier, e.g., "company-a"
    name                  text NOT NULL,                  -- Company display name
    line_channel_secret   text NOT NULL,                  -- LINE Channel Secret
    line_access_token     text NOT NULL,                  -- LINE Channel Access Token
    system_prompt         text DEFAULT '你是一個專業、友善的 AI 客服助理。請根據提供的公司資料回答用戶問題，若資料庫中沒有相關資訊，請如實告知無法回答。',
    logo_url              text,                           -- Company Logo URL
    plan                  text DEFAULT 'basic',           -- Plan (basic, pro, enterprise)
    max_messages_per_month integer DEFAULT 1000,          -- Monthly message quota
    expires_at            timestamptz,                    -- Plan expiration time
    is_active             boolean DEFAULT true,
    created_at            timestamptz DEFAULT now()
);

-- 2. 知識庫表（支援全文搜尋與向量檢索）
CREATE TABLE IF NOT EXISTS knowledge_base (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    title       text NOT NULL,                  -- Knowledge title, e.g., "Return Policy"
    content     text NOT NULL,                  -- Detailed content
    tags        text[],                         -- Tags for categorization
    embedding   vector(768),                    -- 768-dimensional vector (for Gemini text-embedding-004)
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

-- 3. 知識庫索引設計 (B-tree for Tenant Isolation & HNSW for Vector Search)

-- GIN Trigram Index for Chinese-friendly fuzzy text matching
-- Replaces the original to_tsvector('simple', ...) which doesn't support Chinese tokenization.
CREATE INDEX IF NOT EXISTS knowledge_base_trgm_idx
    ON knowledge_base
    USING gin ((title || ' ' || content) gin_trgm_ops);

-- Composite B-tree Index for Tenant Isolation & Status Filtering
-- Speeds up standard lookups and delete cascades
CREATE INDEX IF NOT EXISTS idx_knowledge_base_company_active
    ON knowledge_base (company_id, is_active);

-- Partial HNSW Index for Vector Search (Cosine Distance)
-- m=16: maximum number of connections per node (higher = better recall, more memory)
-- ef_construction=64: search width during index construction (higher = better quality, slower build)
CREATE INDEX IF NOT EXISTS idx_knowledge_base_embedding_hnsw
    ON knowledge_base
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE is_active = true;


-- 4. 對話歷史表
CREATE TABLE IF NOT EXISTS chat_history (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    user_id     text NOT NULL,                  -- LINE User ID
    role        text NOT NULL CHECK (role IN ('user', 'assistant')),
    content     text NOT NULL,
    created_at  timestamptz DEFAULT now()
);

-- 5. 對話歷史索引 (Composite index for tenant query & chronological retrieval)
-- Also covers the foreign key company_id for delete cascades
CREATE INDEX IF NOT EXISTS chat_history_lookup_idx
    ON chat_history (company_id, user_id, created_at DESC);


-- 6. 使用紀錄表
CREATE TABLE IF NOT EXISTS usage_logs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    user_id     text NOT NULL,
    direction   text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    created_at  timestamptz DEFAULT now()
);

-- Index for usage billing and monthly statistics queries
CREATE INDEX IF NOT EXISTS idx_usage_logs_company_created
    ON usage_logs (company_id, created_at DESC);


-- 7. 圖文選單表
CREATE TABLE IF NOT EXISTS company_rich_menus (
    rich_menu_id text PRIMARY KEY,              -- LINE API richMenuId
    company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name         text NOT NULL,
    chat_bar_text text NOT NULL,
    image_url    text NOT NULL,
    areas        jsonb NOT NULL,
    is_active    boolean DEFAULT false,
    created_at   timestamptz DEFAULT now()
);

-- Index for active rich menu lookup per company
CREATE INDEX IF NOT EXISTS idx_company_rich_menus_company_active
    ON company_rich_menus (company_id, is_active);


-- 8. 公司圖文資產表
CREATE TABLE IF NOT EXISTS company_assets (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name        text NOT NULL,
    url         text NOT NULL,
    description text NOT NULL,
    action_type text NOT NULL CHECK (action_type IN ('message', 'uri', 'none')),
    action_value text,
    created_at  timestamptz DEFAULT now()
);

-- Index for company asset listing and delete cascade optimization
CREATE INDEX IF NOT EXISTS idx_company_assets_company_id
    ON company_assets (company_id);


-- 9. 語意快取表 (Semantic Cache)
CREATE TABLE IF NOT EXISTS semantic_cache (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    query_text  text NOT NULL,
    reply_data  text NOT NULL,
    embedding   vector(768),
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

-- Composite B-tree Index for Tenant Isolation & Status Filtering
CREATE INDEX IF NOT EXISTS idx_semantic_cache_company_active
    ON semantic_cache (company_id, is_active);

-- Partial HNSW Index for Fast Semantic Cache Lookup (Cosine Distance)
-- m=16, ef_construction=64: explicitly set for documentation and future tuning
CREATE INDEX IF NOT EXISTS idx_semantic_cache_embedding_hnsw
    ON semantic_cache
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE is_active = true;


-- ============================================
-- Row Level Security (RLS) policies
-- ============================================
ALTER TABLE companies          ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_base     ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_history       ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_logs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_rich_menus ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_assets     ENABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_cache     ENABLE ROW LEVEL SECURITY;

-- Allow service role (backend server) full access
CREATE POLICY "service_role_all" ON companies
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON knowledge_base
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON chat_history
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON usage_logs
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON company_rich_menus
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON company_assets
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON semantic_cache
    FOR ALL USING (auth.role() = 'service_role');


-- ============================================
-- RPC 統計與向量檢索函數
-- ============================================

-- RPC: Count Unique Users for a Company
CREATE OR REPLACE FUNCTION count_unique_users(cid uuid)
RETURNS int
LANGUAGE plpgsql
AS $$
DECLARE
  cnt int;
BEGIN
  SELECT COUNT(DISTINCT user_id) INTO cnt
  FROM chat_history
  WHERE company_id = cid;
  RETURN cnt;
END;
$$;


-- RPC: Vector Search for Knowledge Base
CREATE OR REPLACE FUNCTION match_knowledge (
  query_embedding vector(768),
  match_threshold float,
  match_count int,
  company_filter uuid
)
RETURNS TABLE (
  id uuid,
  title text,
  content text,
  similarity float
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  SELECT
    kb.id,
    kb.title,
    kb.content,
    1 - (kb.embedding <=> query_embedding) AS similarity
  FROM knowledge_base kb
  WHERE kb.is_active = true
    AND kb.company_id = company_filter
    -- Optimizing comparison structure to align with pgvector query planner
    AND (kb.embedding <=> query_embedding) < (1.0 - match_threshold)
  ORDER BY kb.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;


-- Enable pg_trgm extension for Chinese-friendly fuzzy text matching
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- RPC: Hybrid Search (Vector + Trigram Fuzzy Text Match)
-- Uses pg_trgm instead of to_tsvector for Chinese text support.
-- RRF constants: vector=60 (standard), trigram=120 (de-emphasized since trigram is less precise).
CREATE OR REPLACE FUNCTION match_knowledge_hybrid (
  query_embedding vector(768),
  query_text text,
  match_count int,
  company_filter uuid
)
RETURNS TABLE (
  id uuid,
  title text,
  content text,
  similarity float
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  WITH vector_matches AS (
    SELECT
      kb.id,
      ROW_NUMBER() OVER (ORDER BY kb.embedding <=> query_embedding) as rank
    FROM knowledge_base kb
    WHERE kb.is_active = true
      AND kb.company_id = company_filter
    LIMIT match_count * 2
  ),
  trgm_matches AS (
    SELECT
      kb.id,
      ROW_NUMBER() OVER (ORDER BY similarity(kb.title || ' ' || kb.content, query_text) DESC) as rank
    FROM knowledge_base kb
    WHERE kb.is_active = true
      AND kb.company_id = company_filter
      AND similarity(kb.title || ' ' || kb.content, query_text) > 0.1
    LIMIT match_count * 2
  )
  SELECT
    kb.id,
    kb.title,
    kb.content,
    -- RRF fusion: vector weight k=60 (dominant), trigram weight k=120 (supplementary)
    (COALESCE(1.0 / (60 + vm.rank), 0.0) + COALESCE(1.0 / (120 + tm.rank), 0.0))::float AS similarity
  FROM knowledge_base kb
  LEFT JOIN vector_matches vm ON kb.id = vm.id
  LEFT JOIN trgm_matches tm ON kb.id = tm.id
  WHERE (vm.id IS NOT NULL OR tm.id IS NOT NULL)
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$$;


-- RPC: Semantic Cache Lookup
CREATE OR REPLACE FUNCTION match_semantic_cache (
  query_embedding vector(768),
  match_threshold float,
  match_count int,
  company_filter uuid
)
RETURNS TABLE (
  id uuid,
  reply_data text,
  similarity float
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  SELECT
    sc.id,
    sc.reply_data,
    1 - (sc.embedding <=> query_embedding) AS similarity
  FROM semantic_cache sc
  WHERE sc.is_active = true
    AND sc.company_id = company_filter
    -- Optimizing comparison structure to align with pgvector query planner
    AND (sc.embedding <=> query_embedding) < (1.0 - match_threshold)
  ORDER BY sc.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;
