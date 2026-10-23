"""
Step 7: HNSW recall check
=========================
For 10 real questions, compares the HNSW index's top 20 with exact search
(index turned off). Recall is measured on similarity scores, not ids, so tied
duplicate sentences don't count as misses.

Usage:
    python scripts/07_recall_check.py
"""
import importlib.util
import sys

from openai import OpenAI

spec = importlib.util.spec_from_file_location('schema', 'scripts/01_create_schema.py')
schema = importlib.util.module_from_spec(spec)
sys.argv = sys.argv[:1]
spec.loader.exec_module(schema)

QUESTIONS = [
    "How can I find peace during hard times?",
    "What have church leaders taught about prayer?",
    "How do I strengthen my faith in Jesus Christ?",
    "What is the purpose of temples?",
    "How should I treat people who disagree with me?",
    "Why is repentance a joyful thing?",
    "How can families become closer?",
    "What does it mean to minister to others?",
    "How do I recognize personal revelation?",
    "What blessings come from keeping the Sabbath day holy?",
]
K = 20

vectors = OpenAI().embeddings.create(model='text-embedding-3-small', input=QUESTIONS, dimensions=1536).data


def top_scores(vec, exact):
    literal = '[' + ','.join(f'{x:.7f}' for x in vec) + ']'
    setting = "set_config('enable_indexscan', 'off', true)" if exact else "set_config('hnsw.ef_search', '100', true)"
    rows = schema.run_sql(f"""begin;
select {setting};
create temp table r as
select 1 - (embedding <=> '{literal}'::extensions.halfvec(1536)) as sim
from public.sentence_embeddings order by embedding <=> '{literal}'::extensions.halfvec(1536) limit {K};
select sim from r order by sim desc;""")
    schema.run_sql("rollback; select 1")
    return [r['sim'] for r in rows]


total = 0.0
print(f"{'question':55} recall@{K}")
for q, v in zip(QUESTIONS, vectors):
    exact = top_scores(v.embedding, exact=True)
    approx = top_scores(v.embedding, exact=False)
    cutoff = exact[-1] - 1e-6          # anything at least as close as the 20th true neighbor counts
    recall = sum(1 for s in approx if s >= cutoff) / K
    total += recall
    print(f"{q[:55]:55} {recall:.2f}")
print(f"{'mean':55} {total / len(QUESTIONS):.3f}")
