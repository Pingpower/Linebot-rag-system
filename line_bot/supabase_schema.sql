-- ============================================
-- 多租戶 LINE Bot 平台 資料表 Schema
-- 請到 Supabase Dashboard > SQL Editor 執行此檔案
-- ============================================

-- 啟用 vector 擴充功能
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. 公司設定表
CREATE TABLE IF NOT EXISTS companies (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text UNIQUE NOT NULL,           -- URL 識別碼，如 "company-a"
    name        text NOT NULL,                  -- 公司顯示名稱
    line_channel_secret   text NOT NULL,        -- LINE Channel Secret
    line_access_token     text NOT NULL,        -- LINE Channel Access Token
    system_prompt text DEFAULT '你是一個專業、友善的 AI 客服助理。請根據提供的公司資料回答用戶問題，若資料庫中沒有相關資訊，請如實告知無法回答。',
    logo_url    text,                           -- 公司 Logo 圖片網址
    plan        text DEFAULT 'basic',           -- 方案 (basic, pro, enterprise)
    max_messages_per_month integer DEFAULT 1000,-- 每月最大訊息配額
    expires_at  timestamptz,                    -- 方案過期時間
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

-- 2. 知識庫表（支援全文搜尋與向量檢索）
CREATE TABLE IF NOT EXISTS knowledge_base (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    title       text NOT NULL,                  -- 知識標題，如「退貨政策」
    content     text NOT NULL,                  -- 詳細內容
    tags        text[],                         -- 標籤，方便分類
    embedding   vector(3072),                   -- 3072 維度向量 (用於 Gemini Embedding)
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);

-- 3. 為知識庫建立全文搜尋索引（繁中 + 英文）
CREATE INDEX IF NOT EXISTS knowledge_base_fts_idx
    ON knowledge_base
    USING gin(to_tsvector('simple', title || ' ' || content));

-- 4. 對話歷史表
CREATE TABLE IF NOT EXISTS chat_history (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    user_id     text NOT NULL,                  -- LINE 用戶 ID
    role        text NOT NULL CHECK (role IN ('user', 'assistant')),
    content     text NOT NULL,
    created_at  timestamptz DEFAULT now()
);

-- 5. 對話歷史索引（查詢特定用戶的最近記錄）
CREATE INDEX IF NOT EXISTS chat_history_lookup_idx
    ON chat_history (company_id, user_id, created_at DESC);

-- ============================================
-- Row Level Security（資料隔離，防止公司間串資料）
-- ============================================
ALTER TABLE companies      ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_base ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_history   ENABLE ROW LEVEL SECURITY;

-- 允許 service role（後端伺服器）完整存取
CREATE POLICY "service_role_all" ON companies
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON knowledge_base
    FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "service_role_all" ON chat_history
    FOR ALL USING (auth.role() = 'service_role');

-- ============================================
-- 向量搜尋 RPC 函數
-- ============================================
CREATE OR REPLACE FUNCTION match_knowledge (
  query_embedding vector(3072),
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
    AND 1 - (kb.embedding <=> query_embedding) > match_threshold
  ORDER BY kb.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;


CREATE OR REPLACE FUNCTION match_knowledge_hybrid (
  query_embedding vector(3072),
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
  fts_matches AS (
    SELECT
      kb.id,
      ROW_NUMBER() OVER (ORDER BY ts_rank_cd(to_tsvector('simple', kb.title || ' ' || kb.content), plainto_tsquery('simple', query_text)) DESC) as rank
    FROM knowledge_base kb
    WHERE kb.is_active = true
      AND kb.company_id = company_filter
      AND to_tsvector('simple', kb.title || ' ' || kb.content) @@ plainto_tsquery('simple', query_text)
    LIMIT match_count * 2
  )
  SELECT
    kb.id,
    kb.title,
    kb.content,
    (COALESCE(1.0 / (60 + vm.rank), 0.0) + COALESCE(1.0 / (60 + fm.rank), 0.0))::float AS similarity
  FROM knowledge_base kb
  LEFT JOIN vector_matches vm ON kb.id = vm.id
  LEFT JOIN fts_matches fm ON kb.id = fm.id
  WHERE (vm.id IS NOT NULL OR fm.id IS NOT NULL)
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$$;


-- ============================================
-- 追加資料表：使用紀錄、圖文選單、圖文資產、語意快取
-- ============================================

-- 6. 使用紀錄表
CREATE TABLE IF NOT EXISTS usage_logs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    user_id     text NOT NULL,
    direction   text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    created_at  timestamptz DEFAULT now()
);

-- 7. 圖文選單表
CREATE TABLE IF NOT EXISTS company_rich_menus (
    rich_menu_id text PRIMARY KEY,              -- LINE API 回傳的 richMenuId
    company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name         text NOT NULL,
    chat_bar_text text NOT NULL,
    image_url    text NOT NULL,
    areas        jsonb NOT NULL,
    is_active    boolean DEFAULT false,
    created_at   timestamptz DEFAULT now()
);

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

-- 9. 語意快取表
CREATE TABLE IF NOT EXISTS semantic_cache (
    id          bigint PRIMARY KEY,             -- Turbovec index 關聯 ID
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    query_text  text NOT NULL,
    reply_data  text NOT NULL,
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

-- ============================================
-- Row Level Security (RLS) policies
-- ============================================
ALTER TABLE usage_logs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_rich_menus ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_assets     ENABLE ROW LEVEL SECURITY;
ALTER TABLE semantic_cache     ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON usage_logs
    FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_role_all" ON company_rich_menus
    FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_role_all" ON company_assets
    FOR ALL USING (auth.role() = 'service_role');
CREATE POLICY "service_role_all" ON semantic_cache
    FOR ALL USING (auth.role() = 'service_role');

-- ============================================
-- RPC 統計函數
-- ============================================
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



