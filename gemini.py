# -*- coding: utf-8 -*-
"""gemini.py — Gemini calls for cloud jobs, over the whole 6-key pool, by the same rules as the PC's llm.py.

    import gemini
    gemini.JOB, gemini.SOURCE = 'myjob', '성경 구절 삽입'          # where usage lines go / how CapsLock+Q labels them
    model, text = gemini.generate(prompt, ['gemini-3.8-flash', 'gemini-3.6-flash'], task='mytask', units=40)

Keys come from GEMINI_API_KEY_1..6 (repo secrets, entered in the same order as the PC's _api_key1..6.txt,
so the labels key1..key6 mean the same keys in both places).

Rules — each one learned the expensive way on the free tier (memory reference_gemini_api_keys):
- Google counts every request it receives against the day's allowance, a 503 "high demand" included.
- 503 is per MODEL, so every key gets the same answer: give the model up at once, never walk the keys.
- 429 is per key: try the next key. A per-day 429 parks that key for this model for the rest of the run.
- Other 5xx: one short retry on the same key, then the next key. Other 4xx: fail at once.
- The starting key rotates (random per run), so no single key is always spent first.
Every response is logged to jobs/<JOB>/data/_usage.jsonl in the PC's usage.jsonl shape; cloudstate.py
push moves it to the shared cloud log.
"""
import datetime
import json
import os
import random
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent'
JOB = os.environ.get('JOB', '')
SOURCE = os.environ.get('JOB_SOURCE', 'Gemini 관련 (클라우드)')


class GeminiError(Exception):
    pass


def keys():
    """[(label, key)] for every GEMINI_API_KEY_n that is set."""
    return [(f'key{i}', os.environ[f'GEMINI_API_KEY_{i}'].strip()) for i in range(1, 7)
            if os.environ.get(f'GEMINI_API_KEY_{i}', '').strip()]


_START = random.randrange(6)
_PARKED = set()          # (model, label) that answered a per-day 429 during this run


def _log(label, model, r, task, units, **extra):
    if not JOB:
        return
    now = time.time()
    rec = {'ts': int(now), 't': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'key': label, 'model': model, 'r': r, 'src': SOURCE}
    rec.update({k: v for k, v in extra.items() if v is not None})
    if task:
        rec['task'] = task
    if units:
        rec['n'] = units
    d = os.path.join(HERE, 'jobs', JOB, 'data')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, '_usage.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _post(model, key, body, timeout):
    req = urllib.request.Request(ENDPOINT % model, data=body, method='POST',
                                 headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _one_model(model, body, timeout, task, units):
    pool = keys()
    if not pool:
        raise GeminiError('no GEMINI_API_KEY_n in env')
    order = [pool[(_START + j) % len(pool)] for j in range(len(pool))]
    last = None
    for label, key in order:
        if (model, label) in _PARKED:
            continue
        for attempt in (1, 2):
            t0 = time.time()
            try:
                p = _post(model, key, body, timeout)
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', 'ignore')
                dur = int((time.time() - t0) * 1000)
                _log(label, model, 'quota' if e.code == 429 else 'error', task, units,
                     http=e.code, pc=len(body), dur=dur)
                last = f'HTTP {e.code} {model} {label}: {detail[:200]}'
                if e.code == 429:
                    if 'PerDay' in detail:
                        _PARKED.add((model, label))
                    break                                   # next key
                if e.code == 503:
                    raise GeminiError(last)                 # whole model busy: stop, don't walk keys
                if e.code >= 500 and attempt == 1:
                    time.sleep(2)
                    continue                                # one retry, same key
                if e.code >= 500:
                    break
                raise GeminiError(last)                     # other 4xx: a key change won't help
            except Exception as e:                          # timeout / network
                _log(label, model, 'error', task, units, pc=len(body), dur=int((time.time() - t0) * 1000))
                last = f'{type(e).__name__} {model} {label}: {e}'
                break
            um = p.get('usageMetadata') or {}
            cand = (p.get('candidates') or [{}])[0]
            _log(label, model, 'ok', task, units, pt=um.get('promptTokenCount'),
                 ct=um.get('candidatesTokenCount'), tt=um.get('totalTokenCount'),
                 th=um.get('thoughtsTokenCount'), fin=cand.get('finishReason'),
                 pc=len(body), dur=int((time.time() - t0) * 1000))
            if cand.get('finishReason') == 'MAX_TOKENS':
                raise GeminiError(f'{model}: answer cut off (MAX_TOKENS)')
            text = ''.join(part.get('text', '') for part in (cand.get('content') or {}).get('parts', [])
                           if not part.get('thought'))
            return text
    raise GeminiError(last or f'{model}: every key parked for today')


def generate(prompt, models, *, system=None, json_mode=False, temperature=None, max_tokens=None,
             timeout=300, task='', units=None):
    """(model, text) from the first model in `models` that answers; GeminiError if none does."""
    cfg = {}
    if json_mode:
        cfg['responseMimeType'] = 'application/json'
    if temperature is not None:
        cfg['temperature'] = temperature
    if max_tokens:
        cfg['maxOutputTokens'] = max_tokens
    body = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}]}
    if system:
        body['systemInstruction'] = {'parts': [{'text': system}]}
    if cfg:
        body['generationConfig'] = cfg
    raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
    errs = []
    for m in models:
        try:
            return m, _one_model(m, raw, timeout, task, units)
        except GeminiError as e:
            errs.append(str(e))
            print(f'  {m} failed -> next model: {str(e)[:160]}')
    raise GeminiError(' || '.join(errs) or 'no models given')
