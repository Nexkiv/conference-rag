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

import os
import sys
import time

from supabase_admin import (DIMENSIONS, EF_SEARCH, MAX_PAGE_FIELD_CHARS, MAX_QUESTION_CHARS,
                            SUPABASE_URL, run_sql)

# The login check shared by the SECURITY DEFINER search functions.
REQUIRE_LOGIN = """
    IF (SELECT auth.role()) IS DISTINCT FROM 'authenticated' THEN
        RAISE EXCEPTION 'login required' USING ERRCODE = '42501';
    END IF;
    -- HNSW candidates per search. Set here, for this transaction only:
    -- Supabase's postgres role may not attach it to the function definition.
    PERFORM set_config('hnsw.ef_search', '{ef}', true);
""".format(ef=EF_SEARCH)

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

-- Full-text index for smart_search's keyword side. An expression index
-- (instead of a stored tsvector column) avoids rewriting the table and
-- rebuilding the HNSW index.
CREATE INDEX IF NOT EXISTS sentence_embeddings_fts_idx
ON public.sentence_embeddings USING gin (to_tsvector('english', text));

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
    CHECK (length(page_url) <= {MAX_PAGE_FIELD_CHARS} AND length(user_agent) <= {MAX_PAGE_FIELD_CHARS});
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

-- Records a visit, then prunes: rows older than 30 days go (the page shows
-- "views in the last 30 days"), and at most the newest 10,000 stay (so a
-- reload loop can't fill the database).
CREATE OR REPLACE FUNCTION public.record_view(p_url text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.page_views (page_url, user_agent)
    VALUES (left(p_url, {MAX_PAGE_FIELD_CHARS}),
            left(current_setting('request.headers', true)::json ->> 'user-agent', {MAX_PAGE_FIELD_CHARS}));
    DELETE FROM public.page_views WHERE visited_at < now() - interval '30 days';
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
    question TEXT NOT NULL CHECK (length(question) <= {MAX_QUESTION_CHARS}),
    answer TEXT,
    -- Snapshot of what was cited, so history survives a re-import of talks.
    sources JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'error')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- "Deleting" a question hides it: the row stays so the hourly limit still
-- counts it (otherwise deleting history would reset the limit).
ALTER TABLE public.my_questions ADD COLUMN IF NOT EXISTS hidden_at TIMESTAMPTZ;
ALTER TABLE public.my_questions DROP CONSTRAINT IF EXISTS my_questions_question_check;
ALTER TABLE public.my_questions ADD CONSTRAINT my_questions_question_check
    CHECK (length(question) <= {MAX_QUESTION_CHARS});

-- Serves both the history list and the hourly rate-limit count.
CREATE INDEX IF NOT EXISTS my_questions_user_created_idx
ON public.my_questions(user_id, created_at DESC);

ALTER TABLE public.my_questions ENABLE ROW LEVEL SECURITY;

-- Users can read only their own visible rows. There is no INSERT, UPDATE or
-- DELETE policy: claim_question() (called by the generate-answer Edge
-- Function, user_id from the verified login token) creates rows, and
-- hide_question() hides them.
DROP POLICY IF EXISTS "Read own questions" ON public.my_questions;
CREATE POLICY "Read own questions"
ON public.my_questions FOR SELECT
TO authenticated
USING (user_id = (SELECT auth.uid()) AND hidden_at IS NULL);

DROP POLICY IF EXISTS "Delete own questions" ON public.my_questions;
REVOKE ALL ON public.my_questions FROM anon, authenticated;
GRANT SELECT ON public.my_questions TO authenticated;

-- Hides one of the caller's own questions. Returns false if the row isn't
-- theirs (or is already hidden), so nobody can hide someone else's history.
CREATE OR REPLACE FUNCTION public.hide_question(p_id uuid)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    WITH hidden AS (
        UPDATE public.my_questions SET hidden_at = now()
        WHERE id = p_id AND user_id = (SELECT auth.uid()) AND hidden_at IS NULL
        RETURNING 1
    )
    SELECT count(*) > 0 FROM hidden;
$$;
REVOKE ALL ON FUNCTION public.hide_question(uuid) FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.hide_question(uuid) TO authenticated;

-- Rate limit + pending history row in one transaction. The per-user advisory
-- lock makes one user's parallel requests take turns, so a burst can't all
-- read "under the limit" before any of them inserts. Hidden rows still count.
-- Returns the new row id, or NULL when the user is over the limit.
-- Server only: user_id comes from the Edge Function's verified token.
DROP FUNCTION IF EXISTS public.claim_question(uuid, text, int);
CREATE FUNCTION public.claim_question(p_user uuid, p_question text, p_limit int)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    new_id uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext(p_user::text));
    IF (SELECT count(*) FROM public.my_questions
        WHERE user_id = p_user AND created_at > now() - interval '1 hour') >= p_limit THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.my_questions (user_id, question)
    VALUES (p_user, p_question)
    RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;
REVOKE ALL ON FUNCTION public.claim_question(uuid, text, int) FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_question(uuid, text, int) TO service_role;

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
    similarity float
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
#variable_conflict use_column
BEGIN
{REQUIRE_LOGIN}
    RETURN QUERY
    SELECT s.id, s.talk_id, s.title, s.speaker, s.url, s.year, s.season, s.text,
           (1 - (s.embedding <=> query_embedding))::float AS similarity
    FROM public.sentence_embeddings s
    ORDER BY s.embedding <=> query_embedding
    LIMIT least(greatest(match_count, 1), 50);
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
-- talks from winning on volume alone.
-- Returns up to 3 matching sentences per talk (or the opening sentence for a
-- title-only match). SECURITY INVOKER: runs as the caller, so RLS and the
-- column grants above still apply.
-- Whole-word, case-insensitive: the query must not be glued to a letter or
-- digit on either side (regex lookarounds), the same rule as the page's
-- highlighter, so "the" doesn't match "strengthen" and "faith?" still works.
-- Every character that isn't a letter, digit or space is escaped, so the
-- query matches literally; runs of spaces become one. chr(92) is a
-- backslash, written that way to keep backslashes out of this Python string.
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
        SELECT '(?<![[:alnum:]])'
               || regexp_replace(
                      regexp_replace(trim(left(q, {MAX_QUESTION_CHARS})), '[[:space:]]+', ' ', 'g'),
                      '([^[:alnum:][:space:]])', repeat(chr(92), 3) || '1', 'g')
               || '(?![[:alnum:]])' AS p
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

-- ============================================
-- smart_search: hybrid keyword + semantic (Reciprocal Rank Fusion)
-- ============================================
-- Ranks talks two ways, then fuses the RANKS (not the scores, whose scales
-- differ):  score = 1/(60 + semantic_rank) + 1/(60 + keyword_rank)
-- A talk both methods like wins; a talk only one finds still appears, lower.
-- 60 is the standard RRF constant; it keeps rank #1 from dominating.
--   semantic: top 100 sentences by HNSW, talks ranked by sum(similarity)/sqrt(length)
--   keyword:  Postgres full-text search (stemmed, stop words dropped, ANY
--             meaningful word counts), talks ranked by
--             (sum of sentence ts_rank + 5 * title ts_rank) / sqrt(length)
--             so natural-language questions still get a keyword signal.
--             The Keyword panel keeps exact whole-word keyword_search().
-- Same guards as match_sentences (SECURITY DEFINER to read the hidden vectors).
-- Used by the Smart Search panel and by generate-answer to pick RAG talks.
DROP FUNCTION IF EXISTS public.smart_search(text, extensions.halfvec, int);
CREATE FUNCTION public.smart_search(
    q text,
    query_embedding extensions.halfvec({DIMENSIONS}),
    max_talks int DEFAULT 10
)
RETURNS TABLE (
    talk_id uuid,
    title text,
    speaker text,
    url text,
    year int,
    season text,
    score float,
    semantic_rank int,
    keyword_rank int,
    best_similarity float,
    keyword_hits int,
    title_match boolean,
    snippets text[]
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
#variable_conflict use_column
BEGIN
{REQUIRE_LOGIN}
    RETURN QUERY
    WITH sem_hits AS (
        SELECT s.talk_id, s.text, 1 - (s.embedding <=> query_embedding) AS sim
        FROM public.sentence_embeddings s
        ORDER BY s.embedding <=> query_embedding
        LIMIT 100
    ),
    sem AS (
        SELECT h.talk_id,
               sum(h.sim) / sqrt((SELECT count(*) FROM public.sentence_embeddings c
                                  WHERE c.talk_id = h.talk_id)) AS sem_score,
               max(h.sim) AS best_sim,
               (array_agg(h.text ORDER BY h.sim DESC))[1:2] AS snips
        FROM sem_hits h
        GROUP BY h.talk_id
    ),
    sem_ranked AS (
        SELECT sem.*, (rank() OVER (ORDER BY sem.sem_score DESC))::int AS r FROM sem
    ),
    tsq AS (
        -- "find peace in hard times" -> 'find' | 'peac' | 'hard' | 'time'.
        -- Cast the text straight to tsquery: running the stems through
        -- to_tsquery would stem them twice ('agre' -> 'agr') and miss.
        SELECT CASE WHEN numnode(plainto_tsquery('english', q)) = 0 THEN NULL
                    ELSE replace(plainto_tsquery('english', q)::text, ' & ', ' | ')::tsquery
               END AS query
    ),
    fts_hits AS (
        SELECT s.talk_id, s.text, ts_rank(to_tsvector('english', s.text), tsq.query) AS rk
        FROM public.sentence_embeddings s, tsq
        WHERE tsq.query IS NOT NULL
          AND to_tsvector('english', s.text) @@ tsq.query    -- uses sentence_embeddings_fts_idx
    ),
    -- Group hits by talk first, so talk length is looked up once per talk,
    -- not once per matching sentence.
    kw_talks AS (
        SELECT h.talk_id, sum(h.rk) AS rk_sum, count(*)::int AS hits,
               (array_agg(h.text ORDER BY h.rk DESC))[1:2] AS snips
        FROM fts_hits h
        GROUP BY h.talk_id
    ),
    kw AS (
        SELECT k.talk_id,
               (k.rk_sum + 5 * ts_rank(to_tsvector('english', t.title), tsq.query)) / sqrt(t.n) AS kw_score,
               k.hits,
               to_tsvector('english', t.title) @@ tsq.query AS tm,
               k.snips
        FROM kw_talks k
        CROSS JOIN tsq
        CROSS JOIN LATERAL (
            SELECT min(c.title) AS title, count(*) AS n
            FROM public.sentence_embeddings c WHERE c.talk_id = k.talk_id
        ) t
        ORDER BY kw_score DESC
        LIMIT 50
    ),
    kw_ranked AS (
        SELECT kw.*, (rank() OVER (ORDER BY kw.kw_score DESC))::int AS r FROM kw
    ),
    fused AS (
        SELECT coalesce(s.talk_id, k.talk_id) AS tid,
               coalesce(1.0 / (60 + s.r), 0) + coalesce(1.0 / (60 + k.r), 0) AS rrf,
               s.r AS sr, k.r AS kr, s.best_sim, k.hits, k.tm,
               coalesce(s.snips, '{{}}'::text[]) || coalesce(k.snips, '{{}}'::text[]) AS snips
        FROM sem_ranked s
        FULL OUTER JOIN kw_ranked k ON k.talk_id = s.talk_id
        ORDER BY rrf DESC
        LIMIT least(greatest(max_talks, 1), 20)
    )
    SELECT f.tid, t.title, t.speaker, t.url, t.year, t.season,
           f.rrf::float, f.sr, f.kr, f.best_sim::float, coalesce(f.hits, 0), coalesce(f.tm, false),
           -- A sentence can be top for both methods: show it once.
           (ARRAY(SELECT d.x FROM (SELECT DISTINCT ON (u.x) u.x, u.i
                                   FROM unnest(f.snips) WITH ORDINALITY u(x, i)
                                   ORDER BY u.x, u.i) d
                  ORDER BY d.i))[1:3]
    FROM fused f
    CROSS JOIN LATERAL (
        SELECT s.title, s.speaker, s.url, s.year, s.season
        FROM public.sentence_embeddings s WHERE s.talk_id = f.tid LIMIT 1
    ) t
    ORDER BY f.rrf DESC;
END;
$$;
REVOKE ALL ON FUNCTION public.smart_search(text, extensions.halfvec, int) FROM public, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.smart_search(text, extensions.halfvec, int) TO authenticated;

NOTIFY pgrst, 'reload schema';
"""

# Built after the embeddings are loaded: one bulk build is much faster than
# updating the graph on every insert.
# - The free tier's shared memory can't hold a parallel build ("could not
#   resize shared memory segment"), so it builds single-process with 200MB,
#   enough to keep the whole graph in memory.
# - The build takes ~2 minutes, and the Management API cancels statements
#   after 2 minutes, so it runs as a one-off pg_cron job inside the database
#   (no HTTP connection to time out). create_index() waits for it to finish.
INDEX_SQL = """
SET statement_timeout = 0;
SET maintenance_work_mem = '200MB';
SET max_parallel_maintenance_workers = 0;
CREATE INDEX IF NOT EXISTS sentence_embeddings_embedding_hnsw
ON public.sentence_embeddings USING hnsw (embedding extensions.halfvec_cosine_ops);
ANALYZE public.sentence_embeddings;
"""


def create_schema():
    from supabase import create_client

    print("=" * 60)
    print("Creating Database Schema")
    print("=" * 60)

    run_sql(SCHEMA_SQL)
    print("✅ Database schema created successfully!")

    # Verify table exists via PostgREST
    client = create_client(SUPABASE_URL, os.environ['SUPABASE_SERVICE_KEY'])
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
    print("Building HNSW index (cosine) as a one-off pg_cron job...")
    start = time.time()
    run_sql("CREATE EXTENSION IF NOT EXISTS pg_cron;")
    job = run_sql(f"SELECT cron.schedule('build-hnsw', '* * * * *', $job${INDEX_SQL}$job$) AS id")[0]['id']
    try:
        while True:
            time.sleep(15)
            runs = run_sql(f"SELECT status, return_message FROM cron.job_run_details "
                           f"WHERE jobid = {job} ORDER BY start_time LIMIT 1")
            if runs and runs[0]['status'] not in ('starting', 'running'):
                break
    finally:
        run_sql("SELECT cron.unschedule('build-hnsw');")
    if runs[0]['status'] != 'succeeded':
        print(f"❌ Index build {runs[0]['status']}: {runs[0]['return_message']}")
        sys.exit(1)
    size = run_sql("SELECT pg_size_pretty(pg_relation_size('public.sentence_embeddings_embedding_hnsw')) AS index, "
                   "pg_size_pretty(pg_database_size(current_database())) AS database;")
    print(f"✅ Index built in {time.time() - start:.0f}s: {size}")


if __name__ == '__main__':
    if '--index' in sys.argv:
        create_index()
    else:
        create_schema()
        print("\n✅ Schema ready! Next: python scripts/02_scrape_data.py")
