# Conference RAG

A Retrieval Augmented Generation (RAG) application that lets users ask questions about General Conference talks using semantic search and AI-generated answers.

**Live site:** https://nexkiv.github.io/conference-rag/ (magic-link login)

## This fork (CS 452, Fall 2026)

Built on the course starter. What's different:

| Area | Change |
|---|---|
| Data | Talks from 2020–2026 (14 conferences, 501 talks, 30,102 sentences); every listing shows its conference, e.g. "October 2025" |
| Vectors | `text-embedding-3-small` at full 1536 dims stored as `halfvec(1536)` (2-byte floats) + HNSW index: half the size of `vector(1536)`, so data + index fit the 500 MB free tier. HNSW recall@20 vs exact search: **0.99** (`07_recall_check.py`) |
| Keyword search | `keyword_search()`: whole-word, case-insensitive; talks ranked by (matching sentences + 5 × title match) / √(talk length) |
| **Smart Search** | `smart_search()`: semantic (Σ similarity / √length) and full-text (stemmed, any meaningful word, title weighted) talk rankings fused with Reciprocal Rank Fusion (k = 60) |
| RAG | The `generate-answer` Edge Function retrieves the 3 talks itself with Smart Search, sends their full text to GPT-4o, and returns the answer **and** the sources it used; the browser only sends the question |
| **Feature: My Questions** | Private per-user history of RAG questions, answers and cited talks. Users can only read and hide their own rows; only the server writes them |
| Abuse limits | 20 RAG questions per user per hour, checked and recorded atomically in `claim_question()` (per-user lock) before any OpenAI call; hiding history doesn't reset it; 500-character questions; ~12K-token context |
| Secrets | Environment variables only; no `config.secret.json` (steps 01–02's `config.secret.json` instructions don't apply to this fork) |

### Setup differences
- `config.public.json` holds the Supabase URL and the **publishable** key (`sb_publishable_…`); legacy anon/service_role keys are disabled.
- Scripts read `SUPABASE_SERVICE_KEY` (a `sb_secret_…` key), `SUPABASE_ACCESS_TOKEN` and `OPENAI_API_KEY` from the environment; shared settings (model, dimensions, `ef_search`, length limits) live in `scripts/supabase_admin.py`.
- Edge Functions: `supabase secrets set OPENAI_API_KEY="$OPENAI_API_KEY"`; they read the secret key from Supabase's built-in `SUPABASE_SECRET_KEYS`.
- After `05_update_embeddings.py`, build the index with `scripts/01_create_schema.py --index`. The build takes ~2 minutes and the Management API cancels statements after 2 minutes, so the script runs it as a one-off `pg_cron` job and waits for it.
- `scripts/06_security_test.py` checks every access rule and the search behavior with throwaway users (`--rag` adds a real RAG call, `--burst` 25 parallel questions to prove the rate limit holds).

### Access control

| Layer | What it protects |
|---|---|
| Table privileges (GRANT/REVOKE) | anon can't touch `sentence_embeddings` or `my_questions` at all; logged-in users can only SELECT `my_questions` |
| Column privileges | Logged-in users can read every `sentence_embeddings` column **except `embedding`** (vectors are ~97% of the bytes, so nobody can bulk-download them) |
| RLS | `my_questions`: only your own, non-hidden rows; `page_views`: public read |
| `match_sentences()`, `smart_search()` | SECURITY DEFINER so they can read the hidden vectors; check login themselves, anon can't EXECUTE, pinned `search_path`, capped results, never return the vector. ⚠ RLS does not apply inside them: if this table ever holds per-user rows, they must filter by `auth.uid()` themselves |
| `keyword_search()` | SECURITY INVOKER: runs as the caller, so grants and RLS apply; anon can't EXECUTE |
| `hide_question()` | The only way to remove a question from history: hides it if it's yours, so the rate limit still counts it |
| `claim_question()` | Server-only (service role): per-user lock, count, insert; returns nothing when over the limit |
| `record_view()` | The only way to add a page view; keeps the last 30 days and at most 10,000 rows, so anonymous visitors can't fill the database (the page shows views in the last 30 days) |
| Edge Functions | Verify the login with Supabase Auth; search as the user; the OpenAI key never leaves the server; generic errors to the browser, details in logs |
| Browser | Strict CSP, pinned `supabase-js` with SRI, all output escaped. GitHub Pages can't send headers, so no `frame-ancestors` |

**Known limitations:** `match_sentences` and `smart_search` return similarity scores for a caller-chosen vector, so many crafted queries could approximate some stored vectors (the content is public anyway). Keyword search scans every sentence; at millions of rows it needs a trigram or full-text index. Smart Search's `ts_rank` gives common words (e.g. "God") as much title credit as rare ones.

### Scaling notes
- HNSW turns vector search from a full scan into a graph walk; `ef_search = 100`.
- Edge Functions are stateless; GitHub Pages serves the site from a CDN.
- Access rules live in the database, so every client path obeys them.
- Per-user quotas use a per-user advisory lock in Postgres; at higher volume a shared rate limiter (e.g. Redis) would replace it.
- For RAG at scale, send matched passages instead of three full talks (~10K tokens, ~$0.03 per question today).

### Keep-alive switch
Free projects pause after ~7 days idle. `.github/workflows/keepalive.yml` pings a public endpoint daily **only while** the repo variable `KEEPALIVE_UNTIL` (YYYY-MM-DD) is today or later. Turn it on with `gh variable set KEEPALIVE_UNTIL -b <date>`. If the project already paused, click Restore in the Supabase dashboard first (possible for ~90 days).

### Next steps / experiments not done yet
- A `talks` materialized view (one row per talk: metadata + sentence count, refreshed after import) instead of rebuilding talk data from sentences in every search function.
- Local JWKS (ES256) verification in the Edge Functions instead of a round trip to Supabase Auth.
- `pg_trgm` index for keyword search; BM25/IDF weighting so common words count less in Smart Search.
- A similarity gate that skips GPT for off-topic questions.
- Compare Σ/√N against plain Σ ranking on a set of test questions.

---

<p align="center">
  <img src="example.png" alt="Conference RAG screenshot" width="600">
  <br>
  <em>Sample screenshot — semantic search with AI-generated answers</em>
</p>

## 🚀 Quick Start

Follow the step-by-step guides in the `steps/` folder:

```
🏁 LAUNCH ──→ 🔐 SIGN IN ──→ 🔍 KEYWORD ──→ 🧠 SEMANTIC ──→ 🤖 RAG ──→ 🚀 YOURS
```

| Step | Milestone | Topic | Time |
|------|-----------|-------|------|
| [00](steps/00_overview.md) | — | Overview & Environment Setup | 15 min |
| [01](steps/01_fork_and_deploy.md) | 🏁 Launch | Fork & Deploy to GitHub Pages | 15 min |
| [02](steps/02_supabase_and_login.md) | 🔐 Sign In | Supabase Project & Login | 15 min |
| [03](steps/03_database_schema.md) | 🔍 Keyword (1/2) | Database Schema & RLS | 10 min |
| [04](steps/04_scrape_and_import.md) | 🔍 Keyword (2/2) | Scrape & Import → First green light! | 20 min |
| [05](steps/05_embeddings.md) | 🧠 Semantic | Embeddings & Edge Function | 25 min |
| [06](steps/06_rag.md) | 🤖 RAG | RAG Edge Function → All green! | 15 min |
| [07](steps/07_make_it_yours.md) | 🚀 Yours | Add Feature & Reflection | 20 min |

**Prerequisites:**
- [Supabase](https://supabase.com) account (free tier)
- [GitHub](https://github.com) account
- [OpenAI API key](https://platform.openai.com/api-keys) (~$0.60 usage)
- Python 3.9+
- An AI coding assistant (Antigravity, GitHub Copilot, or Cursor)

## 🤖 AI-Assisted Development

This assignment is designed to be completed with an AI coding assistant. See [Step 00](steps/00_overview.md) for setup instructions.

Your AI assistant will automatically read [`ai_agent_instructions.md`](ai_agent_instructions.md) to understand the assignment's learning objectives and teach you along the way.

## 🏗️ Architecture

```
┌─────────────┐
│   Browser   │  Student asks question
│  (GitHub    │
│   Pages)    │
└──────┬──────┘
       │
       ├─── Supabase Auth (magic link)
       │
       ├─── Edge Function: embed-question
       │         ↓ OpenAI API (server-side 🔒)
       │         ↓ Returns embedding vector
       │
       ├─── Supabase Database (pgvector)
       │         ↓ match_sentences()
       │         ↓ Returns similar sentences
       │
       └─── Edge Function: generate-answer
                ↓ GPT-4o (server-side 🔒)
                ↓ Returns final answer
```

## 🔍 Three Search Modes

| Mode | What it does | Requires |
|------|-------------|----------|
| **🔍 Keyword Search** | SQL `ILIKE` query on talk text | Conference data imported |
| **🧠 Semantic Search** | Vector similarity with pgvector | Embeddings + `embed-question` Edge Function |
| **🤖 Ask a Question (RAG)** | AI-generated answers with sources | All Edge Functions deployed |

## 📁 Project Structure

```
conference-rag/
├── index.html                  # Main application UI
├── app.js                      # Keyword, semantic, smart search, RAG, My Questions + auth
├── styles.css                  # Dark theme styling
├── config.js                   # Loads config.public.json for browser
├── config.public.json          # Supabase URL + publishable key (safe to commit)
├── requirements.txt            # Python dependencies
├── .nojekyll                   # Tells GitHub Pages not to use Jekyll
├── steps/                      # Step-by-step assignment guides
│   ├── 00_overview.md
│   ├── ...
│   └── 07_make_it_yours.md
├── scripts/                    # Pipeline scripts (run in order)
│   ├── 01_create_schema.py     # Create DB schema
│   ├── 02_scrape_data.py       # Scrape conference talks → scripts/output/talks.json
│   ├── 03_import_data.py       # Import text to Supabase (🔍 keyword!)
│   ├── 04_embed_data.py        # Generate embeddings → scripts/output/ (💰 saved to disk!)
│   ├── 05_update_embeddings.py # Update DB with embeddings (🧠 semantic!)
│   ├── 06_security_test.py     # Access-control + search-behavior checks with throwaway users
│   ├── 07_recall_check.py      # HNSW recall@20 vs exact search
│   ├── supabase_admin.py       # Shared settings + Management API helper
│   └── output/                 # Intermediate data files (git-ignored)
└── supabase/                   # Edge Functions (YOU create this with supabase init)
    └── functions/
        ├── _shared/            # Shared auth, CORS/JSON, validation & OpenAI helpers
        ├── embed-question/     # Converts questions to embeddings
        └── generate-answer/    # Generates AI answers
```

## 🔒 Security Model

| Component | Security Approach |
|-----------|------------------|
| Supabase publishable key | Safe to expose in `config.public.json` (protected by grants + RLS) |
| OpenAI API key | Server-side only: env var for scripts, Supabase secret for Edge Functions |
| Database access | Row Level Security policies |
| Transport | HTTPS enforced by GitHub Pages |

## 📚 Learning Objectives

1. **Vector Embeddings** — Representing text as searchable numbers
2. **Semantic Search** — Finding similar content with cosine similarity
3. **RAG Architecture** — Combining retrieval + generation
4. **Edge Functions** — Serverless compute for secure API management
5. **Row Level Security** — User-level data access control
6. **Production Deployment** — Full-stack app on GitHub Pages

## 🎓 Assignment Deliverables

1. GitHub repository URL (your fork)
2. Live deployment URL (GitHub Pages)
3. Screenshot of a working query + answer
4. Your custom feature description
5. Written reflection on security, search, and AI-assisted development

## 🆘 Troubleshooting

| Issue | Solution |
|-------|----------|
| "Please configure Supabase" | Update `config.public.json` with your project URL and publishable key |
| Magic link not working | Add your site URL to Supabase → Authentication → URL Configuration |
| Magic link email not arriving | Free tier allows only **3 per hour** — wait and try again, check spam |
| Changes don't appear | Hard refresh (Ctrl+Shift+R) or try incognito window |
| Search shows "Not Ready" | Complete the corresponding step first |
| "Failed to get embedding" | Deploy Edge Functions and set OPENAI_API_KEY secret |
| "Database search failed" | Run `scripts/01_create_schema.py` |
| No search results | Import data with `scripts/03_import_data.py` |

## 📄 License

Educational use only. Conference talk content is used under fair use for educational purposes.

---

Built with ❤️ for CS 452
