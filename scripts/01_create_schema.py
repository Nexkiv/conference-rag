"""
Step 1: Create Database Schema
================================
Creates the tables, Row Level Security policies, column privileges and
database functions in your Supabase database.

Usage:
    python scripts/01_create_schema.py           # schema (safe to re-run)
    python scripts/01_create_schema.py --index   # HNSW index, after embeddings are loaded

Prerequisites:
    - config.public.json with the Supabase URL
    - SUPABASE_ACCESS_TOKEN and SUPABASE_SERVICE_KEY environment variables
"""

import json
import os
import sys
import time

import requests

with open('config.public.json', 'r') as f:
    public_config = json.load(f)

SUPABASE_URL = public_config['SUPABASE_URL']
SUPABASE_PROJECT_REF = SUPABASE_URL.split('//')[1].split('.')[0]
# Secrets come from environment variables only, never from a file.
SUPABASE_SERVICE_KEY = os.environ['SUPABASE_SERVICE_KEY']
SUPABASE_ACCESS_TOKEN = os.environ['SUPABASE_ACCESS_TOKEN']

# Must match DIMENSIONS in 04_embed_data.py and the embed-question Edge Function.
# halfvec stores each number in 2 bytes instead of 4: full 1536 dimensions at half
# the size, so the table + HNSW index fit the 500 MB free tier with room to spare.
DIMENSIONS = 1536

SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

-- ============================================
-- sentence_embeddings: talk sentences + vectors
-- ============================================
CREATE TABLE IF NOT EXISTS public.sentence_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    talk_id UUID NOT NULL,
    title TEXT NOT NULL,
    speaker TEXT,
    calling TEXT,
    year INTEGER,
    season TEXT,
    url TEXT,
    sentence_num INTEGER,
    text TEXT NOT NULL,
    embedding extensions.halfvec({DIMENSIONS}),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS sentence_embeddings_talk_id_idx
ON public.sentence_embeddings(talk_id);

ALTER TABLE public.sentence_embeddings ENABLE ROW LEVEL SECURITY;

-- RLS decides WHICH ROWS: logged-in users can read every sentence.
DROP POLICY IF EXISTS "Allow authenticated users to read" ON public.sentence_embeddings;
CREATE POLICY "Allow authenticated users to read"
ON public.sentence_embeddings FOR SELECT
TO authenticated
USING (true);

-- Column privileges decide WHICH COLUMNS: everything except the embedding.
-- Vectors are ~97% of each row; hiding them stops anyone downloading the
-- whole table's vectors (and burning the free tier's egress).
REVOKE ALL ON public.sentence_embeddings FROM anon, authenticated;
GRANT SELECT (id, talk_id, title, speaker, calling, year, season, url,
              sentence_num, text, created_at)
ON public.sentence_embeddings TO authenticated;

-- ============================================
-- page_views: PUBLIC table (contrast for the RLS demo)
-- ============================================
CREATE TABLE IF NOT EXISTS public.page_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    visited_at TIMESTAMPTZ DEFAULT NOW(),
    page_url TEXT,
    user_agent TEXT
);

ALTER TABLE public.page_views DROP CONSTRAINT IF EXISTS page_views_lengths;
ALTER TABLE public.page_views ADD CONSTRAINT page_views_lengths
    CHECK (length(page_url) <= 500 AND length(user_agent) <= 500);
CREATE INDEX IF NOT EXISTS page_views_visited_at_idx ON public.page_views(visited_at);

ALTER TABLE public.page_views ENABLE ROW LEVEL SECURITY;

-- Anyone can read page views (for the counter).
DROP POLICY IF EXISTS "Allow public reads" ON public.page_views;
CREATE POLICY "Allow public reads"
ON public.page_views FOR SELECT
TO anon, authenticated
USING (true);

-- No INSERT policy: visitors can only add a view through record_view(),
-- which bounds the table so nobody can fill the database with junk rows.
DROP POLICY IF EXISTS "Allow public inserts" ON public.page_views;
REVOKE ALL ON public.page_views FROM anon, authenticated;
GRANT SELECT ON public.page_views TO anon, authenticated;

CREATE OR REPLACE FUNCTION public.record_view(p_url text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.page_views (page_url, user_agent)
    VALUES (left(p_url, 500),
            left(current_setting('request.headers', true)::json ->> 'user-agent', 500));
    -- Keep only the newest 10,000 rows.
    DELETE FROM public.page_views
    WHERE id IN (SELECT id FROM public.page_views ORDER BY visited_at DESC OFFSET 10000);
END;
$$;
REVOKE ALL ON FUNCTION public.record_view(text) FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_view(text) TO anon, authenticated;

-- ============================================
-- my_questions: per-user RAG history (our feature)
-- ============================================
CREATE TABLE IF NOT EXISTS public.my_questions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    question TEXT NOT NULL CHECK (length(question) <= 500),
    answer TEXT,
    -- Snapshot of what was cited, so history survives a re-import of talks.
    sources JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'error')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Serves both the history list and the hourly rate-limit count.
CREATE INDEX IF NOT EXISTS my_questions_user_created_idx
ON public.my_questions(user_id, created_at DESC);

ALTER TABLE public.my_questions ENABLE ROW LEVEL SECURITY;

-- Users can read and delete only their own rows. There is no INSERT or
-- UPDATE policy: only the generate-answer Edge Function writes history, with
-- user_id taken from the verified login token.
DROP POLICY IF EXISTS "Read own questions" ON public.my_questions;
CREATE POLICY "Read own questions"
ON public.my_questions FOR SELECT
TO authenticated
USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS "Delete own questions" ON public.my_questions;
CREATE POLICY "Delete own questions"
ON public.my_questions FOR DELETE
TO authenticated
USING (user_id = (SELECT auth.uid()));

REVOKE ALL ON public.my_questions FROM anon, authenticated;
GRANT SELECT, DELETE ON public.my_questions TO authenticated;

-- ============================================
-- match_sentences: vector similarity search
-- ============================================
-- CAUTION: SECURITY DEFINER runs as the owner, so RLS and the column grants
-- above do NOT apply inside this function. That is what lets it read the
-- hidden embedding column. It is safe only because:
--   1. it checks the caller is logged in before doing anything,
--   2. anon cannot EXECUTE it at all,
--   3. search_path is pinned to schemas API users cannot write to,
--   4. it never returns the embedding and caps results at 50.
-- If this table ever holds per-user rows, this function must filter by
-- auth.uid() itself, or it will leak them across users.
DROP FUNCTION IF EXISTS public.match_sentences(extensions.vector, int);
DROP FUNCTION IF EXISTS public.match_sentences(extensions.halfvec, int);
CREATE FUNCTION public.match_sentences(
    query_embedding extensions.halfvec({DIMENSIONS}),
    match_count int DEFAULT 20
)
RETURNS TABLE (
    id uuid,
    talk_id uuid,
    title text,
    speaker text,
    url text,
    year int,
    season text,
    text text,
    similarity float,
    talk_sentences int
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
#variable_conflict use_column
BEGIN
    IF (SELECT auth.role()) IS DISTINCT FROM 'authenticated' THEN
        RAISE EXCEPTION 'login required' USING ERRCODE = '42501';
    END IF;

    -- HNSW looks at 100 candidates per search (default 40) for better recall.
    -- Set here, for this transaction only: Supabase's postgres role may not
    -- attach this setting to the function definition itself.
    PERFORM set_config('hnsw.ef_search', '100', true);

    RETURN QUERY
    WITH hits AS (
        SELECT s.id, s.talk_id, s.title, s.speaker, s.url, s.year, s.season, s.text,
               1 - (s.embedding <=> query_embedding) AS similarity
        FROM public.sentence_embeddings s
        ORDER BY s.embedding <=> query_embedding
        LIMIT least(greatest(match_count, 1), 50)
    )
    SELECT h.id, h.talk_id, h.title, h.speaker, h.url, h.year, h.season, h.text, h.similarity::float,
           -- Talk length, so the browser can rank talks by sum / sqrt(length).
           (SELECT count(*)::int FROM public.sentence_embeddings c WHERE c.talk_id = h.talk_id)
    FROM hits h
    ORDER BY h.similarity DESC;
END;
$$;
REVOKE ALL ON FUNCTION public.match_sentences(extensions.halfvec, int) FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.match_sentences(extensions.halfvec, int) TO authenticated;

-- ============================================
-- keyword_search: whole-word search, ranked per talk
-- ============================================
-- Each talk that uses the words (or has them in its title) gets
--     score = (matching sentences + 5 * title_match) / sqrt(talk sentences)
-- so a title match is worth 5 extra mentions: it helps, but a talk that
-- uses the words much more often can still outrank it. The sqrt keeps long
-- talks from winning on volume alone (same idea as the RAG ranking).
-- Returns up to 3 matching sentences per talk (or the opening sentence for a
-- title-only match). SECURITY INVOKER: runs as the caller, so RLS and the
-- column grants above still apply.
-- Matching is whole-word and case-insensitive: a regex ~* wrapped in the
-- word-boundary escapes m and M (start/end of word), so "the" no longer matches "strengthen". Every
-- character that isn't a letter, digit or space is escaped, so the query
-- matches literally. chr(92) is a backslash, written that way to keep
-- backslashes out of this Python string.
DROP FUNCTION IF EXISTS public.keyword_search(text, int);
CREATE FUNCTION public.keyword_search(q text, max_talks int DEFAULT 10)
RETURNS TABLE (
    talk_id uuid,
    title text,
    speaker text,
    url text,
    year int,
    season text,
    text text,
    title_match boolean,
    hits int,
    score float
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    WITH pat AS (
        SELECT chr(92) || 'm'
               || regexp_replace(trim(left(q, 500)), '([^[:alnum:][:space:]])', repeat(chr(92), 3) || '1', 'g')
               || chr(92) || 'M' AS p
    ),
    matches AS (
        SELECT s.talk_id, s.sentence_num, s.text
        FROM public.sentence_embeddings s, pat
        WHERE s.text ~* pat.p
    ),
    talks AS (
        SELECT s.talk_id,
               min(s.title) AS title, min(s.speaker) AS speaker, min(s.url) AS url,
               min(s.year) AS year, min(s.season) AS season,
               count(*) AS n,
               bool_or(s.title ~* pat.p) AS title_match
        FROM public.sentence_embeddings s, pat
        WHERE s.talk_id IN (SELECT m.talk_id FROM matches m) OR s.title ~* pat.p
        GROUP BY s.talk_id
    ),
    ranked AS (
        SELECT t.*, h.hits,
               ((h.hits + 5 * t.title_match::int) / sqrt(t.n))::float AS score
        FROM talks t
        CROSS JOIN LATERAL (
            SELECT count(*)::int AS hits FROM matches m WHERE m.talk_id = t.talk_id
        ) h
        ORDER BY score DESC, t.year DESC
        LIMIT least(greatest(max_talks, 1), 20)
    )
    SELECT r.talk_id, r.title, r.speaker, r.url, r.year, r.season, x.text, r.title_match, r.hits, r.score
    FROM ranked r
    CROSS JOIN LATERAL (
        (SELECT m.text, m.sentence_num FROM matches m
         WHERE m.talk_id = r.talk_id ORDER BY m.sentence_num LIMIT 3)
        UNION ALL
        (SELECT s.text, s.sentence_num FROM public.sentence_embeddings s
         WHERE s.talk_id = r.talk_id AND r.hits = 0 ORDER BY s.sentence_num LIMIT 1)
    ) x
    ORDER BY r.score DESC, r.year DESC, r.talk_id, x.sentence_num;
$$;
REVOKE ALL ON FUNCTION public.keyword_search(text, int) FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.keyword_search(text, int) TO authenticated;

NOTIFY pgrst, 'reload schema';
"""

# Built after the embeddings are loaded: one bulk build is much faster than
# updating the graph on every insert.
# The free tier's shared memory can't hold a big parallel build ("could not
# resize shared memory segment"), so build single-process. 200MB holds the
# whole graph in memory, which keeps the build under the API's ~100 s timeout.
INDEX_SQL = """
SET maintenance_work_mem = '200MB';
SET max_parallel_maintenance_workers = 0;
CREATE INDEX IF NOT EXISTS sentence_embeddings_embedding_hnsw
ON public.sentence_embeddings USING hnsw (embedding extensions.halfvec_cosine_ops);
ANALYZE public.sentence_embeddings;
"""


def run_sql(sql):
    """Run SQL as the database owner through the Supabase Management API."""
    resp = requests.post(
        f"https://api.supabase.com/v1/projects/{SUPABASE_PROJECT_REF}/database/query",
        headers={"Authorization": f"Bearer {SUPABASE_ACCESS_TOKEN}",
                 "Content-Type": "application/json"},
        json={"query": sql},
        timeout=600,
    )
    if resp.status_code not in (200, 201):
        print(f"❌ SQL failed: {resp.status_code}")
        print(resp.text[:1000])
        sys.exit(1)
    return resp.json()


def create_schema():
    from supabase import create_client

    print("=" * 60)
    print("Creating Database Schema")
    print("=" * 60)

    run_sql(SCHEMA_SQL)
    print("✅ Database schema created successfully!")

    # Verify table exists via PostgREST
    client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    for attempt in range(5):
        try:
            result = client.table('sentence_embeddings').select('id', count='exact').limit(1).execute()
            print(f"✅ Table verified. Current rows: {result.count or 0}")
            return
        except Exception:
            if attempt < 4:
                print(f"   Waiting for schema cache to refresh... ({attempt + 1}/5)")
                time.sleep(3)
    print("⚠️  Table created but PostgREST cache hasn't refreshed yet.")
    print("   This is normal — it should be ready by import time.")


def create_index():
    print("Building HNSW index (cosine)...")
    start = time.time()
    run_sql(INDEX_SQL)
    size = run_sql("SELECT pg_size_pretty(pg_relation_size('public.sentence_embeddings_embedding_hnsw')) AS index, "
                   "pg_size_pretty(pg_database_size(current_database())) AS database;")
    print(f"✅ Index built in {time.time() - start:.0f}s: {size}")


if __name__ == '__main__':
    if '--index' in sys.argv:
        create_index()
    else:
        create_schema()
        print("\n✅ Schema ready! Next: python scripts/02_scrape_data.py")
