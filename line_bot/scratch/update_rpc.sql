CREATE OR REPLACE FUNCTION match_knowledge_hybrid (
  query_embedding vector(768),
  query_text text,
  match_count int,
  company_filter uuid,
  filter_tags text[] DEFAULT NULL
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
      AND (filter_tags IS NULL OR kb.tags @> filter_tags)
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
      AND (filter_tags IS NULL OR kb.tags @> filter_tags)
    LIMIT match_count * 2
  )
  SELECT
    kb.id,
    kb.title,
    kb.content,
    (COALESCE(1.0 / (60 + vm.rank), 0.0) + COALESCE(1.0 / (120 + tm.rank), 0.0))::float AS similarity
  FROM knowledge_base kb
  LEFT JOIN vector_matches vm ON kb.id = vm.id
  LEFT JOIN trgm_matches tm ON kb.id = tm.id
  WHERE (vm.id IS NOT NULL OR tm.id IS NOT NULL)
  ORDER BY similarity DESC
  LIMIT match_count;
END;
$$;
