"""
Shared settings and the Management API helper used by the scripts.

Secrets are read only when a function needs them, so importing this module
never requires a key the importing script doesn't use.
"""

import json
import os
import sys

import requests

with open('config.public.json', 'r') as f:
    SUPABASE_URL = json.load(f)['SUPABASE_URL']
SUPABASE_PROJECT_REF = SUPABASE_URL.split('//')[1].split('.')[0]

# Embeddings. Must match supabase/functions/_shared/openai.ts.
# halfvec stores each number in 2 bytes instead of 4: full 1536 dimensions at half
# the size, so the table + HNSW index fit the 500 MB free tier with room to spare.
EMBED_MODEL = 'text-embedding-3-small'
DIMENSIONS = 1536

# HNSW candidates per search (pgvector default 40). Higher = better recall, slower.
EF_SEARCH = 100

# Must match MAX_QUESTION_CHARS in supabase/functions/_shared/validate.ts
# and maxlength="..." on the inputs in index.html.
MAX_QUESTION_CHARS = 500
# page_views fields (a separate rule from the question limit).
MAX_PAGE_FIELD_CHARS = 500


def run_sql(sql):
    """Run SQL as the database owner through the Supabase Management API.

    Returns the rows of the last statement. Exits with the API's message on failure.
    """
    resp = requests.post(
        f"https://api.supabase.com/v1/projects/{SUPABASE_PROJECT_REF}/database/query",
        headers={"Authorization": f"Bearer {os.environ['SUPABASE_ACCESS_TOKEN']}",
                 "Content-Type": "application/json"},
        json={"query": sql},
        timeout=600,
    )
    if resp.status_code not in (200, 201):
        print(f"❌ SQL failed: {resp.status_code}")
        print(resp.text[:1000])
        sys.exit(1)
    return resp.json()
