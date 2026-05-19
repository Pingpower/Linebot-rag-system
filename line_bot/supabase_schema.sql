-- ============================================
-- 多租戶 LINE Bot 平台 資料表 Schema
-- 請到 Supabase Dashboard > SQL Editor 執行此檔案
-- ============================================

-- 1. 公司設定表
CREATE TABLE IF NOT EXISTS companies (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text UNIQUE NOT NULL,           -- URL 識別碼，如 "company-a"
    name        text NOT NULL,                  -- 公司顯示名稱
    line_channel_secret   text NOT NULL,        -- LINE Channel Secret
    line_access_token     text NOT NULL,        -- LINE Channel Access Token
    system_prompt text DEFAULT '你是一個專業、友善的 AI 客服助理。請根據提供的公司資料回答用戶問題，若資料庫中沒有相關資訊，請如實告知無法回答。',
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

-- 2. 知識庫表（支援全文搜尋）
CREATE TABLE IF NOT EXISTS knowledge_base (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    title       text NOT NULL,                  -- 知識標題，如「退貨政策」
    content     text NOT NULL,                  -- 詳細內容
    tags        text[],                         -- 標籤，方便分類
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
-- 範例資料（測試用，執行後可刪除）
-- ============================================
-- INSERT INTO companies (slug, name, line_channel_secret, line_access_token, system_prompt)
-- VALUES (
--     'demo-company',
--     '示範公司',
--     'your_channel_secret_here',
--     'your_access_token_here',
--     '你是示範公司的專屬客服，請根據公司資料回答問題。'
-- );
