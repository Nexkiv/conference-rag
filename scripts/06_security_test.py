"""
Step 6: Security test
=====================
Checks every access rule with real login tokens. Creates two throwaway users
(A and B) with the secret key, signs each in, runs the checks, then deletes
both users and their history.

Usage:
    python scripts/06_security_test.py            # access-control checks
    python scripts/06_security_test.py --rag      # also one real RAG call as A (~$0.03)

Prerequisites:
    - config.public.json (URL + publishable key)
    - SUPABASE_SERVICE_KEY environment variable
"""

import json
import os
import secrets
import sys

import requests
from supabase import create_client

with open('config.public.json') as f:
    public = json.load(f)
URL = public['SUPABASE_URL']
PUBLISHABLE = public['SUPABASE_PUBLISHABLE_KEY']
admin = create_client(URL, os.environ['SUPABASE_SERVICE_KEY'])

results = []


def check(name, passed, detail=''):
    results.append(passed)
    print(f"{'✅' if passed else '❌'} {name}" + (f"  ({detail})" if detail else ''))


def denied(fn):
    """True if the call raises a permission error."""
    try:
        fn()
        return False
    except Exception as e:
        return 'permission denied' in str(e) or '42501' in str(e)


def make_user(label):
    email = f"rls-test-{label}-{secrets.token_hex(4)}@example.com"
    password = secrets.token_urlsafe(24)
    user = admin.auth.admin.create_user({'email': email, 'password': password, 'email_confirm': True}).user
    client = create_client(URL, PUBLISHABLE)
    session = client.auth.sign_in_with_password({'email': email, 'password': password}).session
    return user, client, session.access_token


def call_function(name, body, token=None):
    headers = {'apikey': PUBLISHABLE, 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return requests.post(f"{URL}/functions/v1/{name}", headers=headers, json=body, timeout=120)


def main():
    anon = create_client(URL, PUBLISHABLE)
    user_a, client_a, token_a = make_user('a')
    user_b, client_b, token_b = make_user('b')
    try:
        print("--- Database: anonymous visitor (publishable key only)")
        check("anon cannot read sentence_embeddings",
              denied(lambda: anon.table('sentence_embeddings').select('text').limit(1).execute()))
        check("anon cannot call match_sentences",
              denied(lambda: anon.rpc('match_sentences', {'query_embedding': [0.0] * 1536}).execute()))
        check("anon cannot call smart_search",
              denied(lambda: anon.rpc('smart_search', {'q': 'faith', 'query_embedding': [0.0] * 1536}).execute()))
        check("anon cannot read my_questions",
              denied(lambda: anon.table('my_questions').select('*').execute()))
        check("anon cannot insert into page_views directly",
              denied(lambda: anon.table('page_views').insert({'page_url': 'x'}).execute()))
        anon.rpc('record_view', {'p_url': 'security-test'}).execute()
        check("anon can record a view through record_view()", True)
        check("anon can read page_views",
              len(anon.table('page_views').select('id').limit(1).execute().data) == 1)

        print("--- Database: logged-in user")
        rows = client_a.table('sentence_embeddings').select('title, text').limit(1).execute().data
        check("user can read sentence text", len(rows) == 1)
        check("user cannot read the embedding column",
              denied(lambda: client_a.table('sentence_embeddings').select('embedding').limit(1).execute()))
        check("user cannot select * (includes embedding)",
              denied(lambda: client_a.table('sentence_embeddings').select('*').limit(1).execute()))
        hits = client_a.rpc('match_sentences', {'query_embedding': [0.01] * 1536, 'match_count': 999}).execute().data
        check("match_sentences caps results at 50", len(hits) <= 50, f"{len(hits)} rows")
        check("match_sentences never returns embeddings", all('embedding' not in h for h in hits))
        smart = client_a.rpc('smart_search', {'q': 'faith', 'query_embedding': [0.01] * 1536, 'max_talks': 999}).execute().data
        check("smart_search caps results at 20 talks", len(smart) <= 20, f"{len(smart)} talks")
        check("smart_search never returns embeddings", all('embedding' not in h for h in smart))

        print("--- Edge Functions")
        r = call_function('embed-question', {'question': 'faith'})
        check("embed-question without login -> 401", r.status_code == 401, r.status_code)
        r = call_function('embed-question', {'question': 'faith'}, 'not-a-real-token')
        check("embed-question with forged token -> 401", r.status_code == 401, r.status_code)
        r = call_function('generate-answer', {'ping': True}, token_a)
        check("generate-answer ping with login -> 200", r.status_code == 200, r.status_code)
        r = call_function('embed-question', {'question': 'x' * 600}, token_a)
        check("embed-question 600-char question -> 400", r.status_code == 400, r.status_code)
        r = call_function('generate-answer', {'question': 'x' * 600, 'context_talks': []}, token_a)
        check("generate-answer 600-char question -> 400", r.status_code == 400, r.status_code)

        print("--- my_questions (per-user RLS)")
        # Seed A's history the way the Edge Function does (secret key, user_id from the server).
        admin.table('my_questions').insert({'user_id': user_a.id, 'question': 'A private question',
                                            'answer': 'A private answer', 'status': 'done'}).execute()
        check("A sees own history",
              len(client_a.table('my_questions').select('id').execute().data) == 1)
        check("B cannot see A's history",
              len(client_b.table('my_questions').select('id').execute().data) == 0)
        client_b.table('my_questions').delete().eq('user_id', user_a.id).execute()
        check("B cannot delete A's history",
              len(admin.table('my_questions').select('id').eq('user_id', user_a.id).execute().data) == 1)
        check("B cannot insert a row as A",
              denied(lambda: client_b.table('my_questions').insert(
                  {'user_id': user_a.id, 'question': 'forged', 'status': 'done'}).execute()))
        check("A cannot insert even as A (only the server writes)",
              denied(lambda: client_a.table('my_questions').insert(
                  {'user_id': user_a.id, 'question': 'forged', 'status': 'done'}).execute()))
        check("A cannot edit own history",
              denied(lambda: client_a.table('my_questions').update({'answer': 'edited'})
                     .eq('user_id', user_a.id).execute()))

        print("--- Rate limit")
        admin.table('my_questions').insert([{'user_id': user_b.id, 'question': f'q{i}', 'status': 'done'}
                                            for i in range(20)]).execute()
        r = call_function('generate-answer', {'question': 'What is faith?', 'context_talks': []}, token_b)
        check("21st question in an hour -> 429", r.status_code == 429, r.status_code)

        if '--rag' in sys.argv:
            print("--- Real RAG call as A")
            r = call_function('generate-answer', {'question': 'What is faith?', 'context_talks': [
                {'title': 'Test Talk', 'speaker': 'Test Speaker', 'text': 'Faith is trust in God.'}]}, token_a)
            check("generate-answer returns an answer", r.status_code == 200 and 'answer' in r.json(), r.status_code)
            saved = client_a.table('my_questions').select('question, status, sources').eq(
                'question', 'What is faith?').execute().data
            check("the answer was saved to A's history as done",
                  len(saved) == 1 and saved[0]['status'] == 'done', saved)
    finally:
        for u in (user_a, user_b):
            admin.auth.admin.delete_user(u.id)  # ON DELETE CASCADE removes their history
        print("(test users deleted)")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == '__main__':
    main()
