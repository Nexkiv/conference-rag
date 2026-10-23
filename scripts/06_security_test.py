"""
Step 6: Security test
=====================
Checks every access rule with real login tokens. Creates two throwaway users
(A and B) with the secret key, signs each in, runs the checks, then deletes
both users and their history.

Usage:
    python scripts/06_security_test.py            # access-control + search-behavior checks
    python scripts/06_security_test.py --rag      # also one real RAG call as A (~$0.03)
    python scripts/06_security_test.py --burst    # also 25 parallel RAG calls (~$0.60): at most 20 may pass

Prerequisites:
    - config.public.json (URL + publishable key)
    - SUPABASE_SERVICE_KEY environment variable
"""

import json
import os
import re
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor

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


def succeeds(fn):
    """True if the call runs without raising."""
    try:
        fn()
        return True
    except Exception:
        return False


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
        check("anon cannot call keyword_search",
              denied(lambda: anon.rpc('keyword_search', {'q': 'faith'}).execute()))
        check("anon cannot read my_questions",
              denied(lambda: anon.table('my_questions').select('*').execute()))
        check("anon cannot insert into page_views directly",
              denied(lambda: anon.table('page_views').insert({'page_url': 'x'}).execute()))
        check("anon cannot update page_views",
              denied(lambda: anon.table('page_views').update({'page_url': 'x'}).neq('page_url', '').execute()))
        check("anon cannot delete page_views",
              denied(lambda: anon.table('page_views').delete().neq('page_url', '').execute()))
        check("anon can record a view through record_view()",
              succeeds(lambda: anon.rpc('record_view', {'p_url': 'security-test'}).execute()))
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

        print("--- Search behavior")
        kw = lambda q: client_a.rpc('keyword_search', {'q': q}).execute().data
        check("keyword_search matches 'faith?' (punctuation at the edge)", len(kw('faith?')) > 0)
        whole_the = re.compile(r'(?<![^\W_])the(?![^\W_])', re.I)  # not glued to a letter or digit
        rows = kw('the')
        check("keyword_search 'the' only matches the whole word",
              all(whole_the.search(r['text']) for r in rows if r['hits'] > 0), f"{len(rows)} rows")
        check("keyword_search marks title matches", any(r['title_match'] for r in kw('faith')))
        agreed = client_a.rpc('smart_search', {'q': 'agreed', 'query_embedding': [0.01] * 1536}).execute().data
        check("smart_search finds keyword matches for 'agreed' (no double stemming)",
              any(r['keyword_rank'] for r in agreed))

        print("--- Edge Functions")
        r = call_function('embed-question', {'question': 'faith'})
        check("embed-question without login -> 401", r.status_code == 401, r.status_code)
        r = call_function('embed-question', {'question': 'faith'}, 'not-a-real-token')
        check("embed-question with forged token -> 401", r.status_code == 401, r.status_code)
        r = call_function('embed-question', {'ping': True}, token_a)
        check("embed-question ping with login -> 200", r.status_code == 200, r.status_code)
        r = call_function('generate-answer', {'ping': True}, token_a)
        check("generate-answer ping with login -> 200", r.status_code == 200, r.status_code)
        r = call_function('embed-question', {'question': 'x' * 600}, token_a)
        check("embed-question 600-char question -> 400", r.status_code == 400, r.status_code)
        r = call_function('generate-answer', {'question': 'x' * 600}, token_a)
        check("generate-answer 600-char question -> 400", r.status_code == 400, r.status_code)

        print("--- my_questions (per-user RLS)")
        # Seed A's history the way the Edge Function does (secret key, user_id from the server).
        admin.table('my_questions').insert({'user_id': user_a.id, 'question': 'A private question',
                                            'answer': 'A private answer', 'status': 'done'}).execute()
        check("A sees own history",
              len(client_a.table('my_questions').select('id').execute().data) == 1)
        check("B cannot see A's history",
              len(client_b.table('my_questions').select('id').execute().data) == 0)
        a_row = client_a.table('my_questions').select('id').execute().data[0]['id']
        check("B cannot delete A's history",
              denied(lambda: client_b.table('my_questions').delete().eq('user_id', user_a.id).execute()))
        check("B cannot hide A's history",
              client_b.rpc('hide_question', {'p_id': a_row}).execute().data is False
              and len(client_a.table('my_questions').select('id').execute().data) == 1)
        check("A cannot delete rows directly (only hide)",
              denied(lambda: client_a.table('my_questions').delete().eq('id', a_row).execute()))
        check("A can hide own question, and it disappears from A's history",
              client_a.rpc('hide_question', {'p_id': a_row}).execute().data is True
              and client_a.table('my_questions').select('id').execute().data == [])
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
        seeded = admin.table('my_questions').insert([{'user_id': user_b.id, 'question': f'q{i}', 'status': 'done'}
                                                     for i in range(20)]).execute().data
        r = call_function('generate-answer', {'question': 'What is faith?'}, token_b)
        check("21st question in an hour -> 429", r.status_code == 429, r.status_code)
        for row in seeded:
            client_b.rpc('hide_question', {'p_id': row['id']}).execute()
        r = call_function('generate-answer', {'question': 'What is faith?'}, token_b)
        check("hiding all 20 does not reset the limit -> still 429", r.status_code == 429, r.status_code)

        if '--rag' in sys.argv:
            print("--- Real RAG call as A (server-side retrieval)")
            r = call_function('generate-answer', {'question': 'What is faith?'}, token_a)
            body = r.json()
            check("generate-answer returns an answer and its sources",
                  r.status_code == 200 and body.get('answer') and len(body.get('sources', [])) > 0, r.status_code)
            saved = client_a.table('my_questions').select('status, sources').eq(
                'question', 'What is faith?').execute().data
            check("the answer and sources were saved to A's history as done",
                  len(saved) == 1 and saved[0]['status'] == 'done' and saved[0]['sources'] == body.get('sources'),
                  saved and saved[0]['status'])

        if '--burst' in sys.argv:
            print("--- Burst: 25 parallel questions from a fresh user")
            user_c, _, token_c = make_user('c')
            try:
                with ThreadPoolExecutor(max_workers=25) as pool:
                    codes = list(pool.map(lambda i: call_function(
                        'generate-answer', {'question': f'What is faith? ({i})'}, token_c).status_code, range(25)))
                passed = codes.count(200)
                check("at most 20 of 25 parallel questions get through", passed <= 20,
                      f"{passed} x 200, {codes.count(429)} x 429, other {[c for c in codes if c not in (200, 429)]}")
            finally:
                admin.auth.admin.delete_user(user_c.id)
    finally:
        for u in (user_a, user_b):
            admin.auth.admin.delete_user(u.id)  # ON DELETE CASCADE removes their history
        print("(test users deleted)")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == '__main__':
    main()
