# Conference RAG

A Retrieval Augmented Generation (RAG) application that lets users ask questions about General Conference talks using semantic search and AI-generated answers.

**Live site:** https://nexkiv.github.io/conference-rag/ (magic-link login)

## This fork (CS 452, Fall 2026)

Built on the course starter. What's different:

| Area | Change |
|---|---|
| Data | Talks from 2020–2026 (14 conferences, 501 talks, 30,102 sentences); every listing shows its conference, e.g. "October 2025" |
| Vectors | `text-embedding-3-small` at full 1536 dims stored as `halfvec(1536)` (2-byte floats) + HNSW index: half the size of `vector(1536)`, so data + index fit the 500 MB free tier |
| Keyword search | `keyword_search()`: whole-word, case-insensitive; talks ranked by (matching sentences + 5 × title match) / √(talk length) |
| RAG ranking | Talks ranked by Σ similarity / √(talk length) instead of average similarity |
| **Feature: My Questions** | Private per-user history of RAG questions, answers and cited talks, with delete. RLS: `user_id = auth.uid()` for SELECT/DELETE; no INSERT/UPDATE policy, so only the Edge Function writes rows |
| Abuse limits | 20 RAG questions per user per hour (pending row written before the GPT call; fails closed), 500-character questions, ~12K-token context |
| Secrets | Environment variables only; no `config.secret.json` |

### Setup differences
- `config.public.json` holds the Supabase URL and the **publishable** key (`sb_publishable_…`); legacy anon/service_role keys are disabled.
- Scripts read `SUPABASE_SERVICE_KEY` (a `sb_secret_…` key), `SUPABASE_ACCESS_TOKEN` and `OPENAI_API_KEY` from the environment.
- Edge Functions: `supabase secrets set OPENAI_API_KEY="$OPENAI_API_KEY"`; they read the secret key from Supabase's built-in `SUPABASE_SECRET_KEYS`.
- After `05_update_embeddings.py`, build the index with `scripts/01_create_schema.py --index`. The Management API cancels statements after 2 minutes, so on the free tier schedule the build once with `pg_cron` instead (see the script).
- `scripts/06_security_test.py` checks every access rule with two throwaway users.

### Access control

| Layer | What it protects |
|---|---|
| Table privileges (GRANT/REVOKE) | anon can't touch `sentence_embeddings` or `my_questions` at all |
| Column privileges | Logged-in users can read every `sentence_embeddings` column **except `embedding`** (vectors are ~97% of the bytes, so nobody can bulk-download them) |
| RLS | `my_questions`: only your own rows; `page_views`: public read |
| `match_sentences()` | SECURITY DEFINER so it can read the hidden vectors; checks login itself, anon can't EXECUTE, pinned `search_path`, returns ≤ 50 rows and never the vector. ⚠ RLS does not apply inside it: if this table ever holds per-user rows, it must filter by `auth.uid()` itself |
| `record_view()` | The only way to add a page view; caps the table at the newest 10,000 rows so anonymous visitors can't fill the database |
| Edge Functions | Verify the login with Supabase Auth; the OpenAI key never leaves the server; generic errors to the browser, details in logs |
| Browser | Strict CSP, pinned `supabase-js` with SRI, all output escaped. GitHub Pages can't send headers, so no `frame-ancestors` |

**Known limitations:** `match_sentences` returns similarity scores for a caller-chosen vector, so many crafted queries could approximate some stored vectors (the content is public anyway). Keyword search scans every sentence; at millions of rows it needs a trigram or full-text index.

### Scaling notes
- HNSW turns vector search from a full scan into a graph walk; `ef_search = 100`.
- Edge Functions are stateless; GitHub Pages serves the site from a CDN.
- Access rules live in the database, so every client path obeys them.
- Per-user quotas live in the same table as the feature; at higher volume a shared rate limiter (e.g. Redis) would replace the count query.
- For RAG at scale, send matched passages instead of three full talks (~10K tokens, ~$0.03 per question today).

### Keep-alive switch
Free projects pause after ~7 days idle. `.github/workflows/keepalive.yml` pings a public endpoint daily **only while** the repo variable `KEEPALIVE_UNTIL` (YYYY-MM-DD) is today or later. Turn it on with `gh variable set KEEPALIVE_UNTIL -b <date>`. If the project already paused, click Restore in the Supabase dashboard first (possible for ~90 days).

### Experiments not done yet
Local JWKS (ES256) verification in the Edge Functions · `pg_trgm` index for keyword search · a similarity gate that skips GPT for off-topic questions · HNSW recall vs exact search and Σ/√N vs Σ ranking (flagged for evaluation)

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
├── app.js                      # Three search modes + auth logic
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
│   ├── 06_security_test.py     # Access-control checks with two throwaway users
│   └── output/                 # Intermediate data files (git-ignored)
└── supabase/                   # Edge Functions (YOU create this with supabase init)
    └── functions/
        ├── _shared/            # Shared auth & CORS helpers
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
