# -*- coding: utf-8 -*-
"""selftest — proves the runner works without spending Gemini quota (unless asked to).

1. which GEMINI_API_KEY_n secrets are present (count and labels only — never the values);
2. the private bucket round-trip: data/selftest.json carries a run counter, pulled before and pushed after;
3. with --call (workflow input `call`): ONE tiny Gemini request, which also proves usage logging.
"""
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import gemini  # noqa: E402

gemini.JOB, gemini.SOURCE = 'selftest', 'Gemini 관련 (클라우드)'
DATA = os.path.join(ROOT, 'jobs', 'selftest', 'data')

labels = [lab for lab, _ in gemini.keys()]
print(f'Gemini keys present: {len(labels)} ({", ".join(labels) or "none"})')

os.makedirs(DATA, exist_ok=True)
p = os.path.join(DATA, 'selftest.json')
st = json.load(open(p, encoding='utf-8')) if os.path.exists(p) else {'runs': 0}
st['runs'] += 1
st['last'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
st['keys'] = len(labels)
json.dump(st, open(p, 'w', encoding='utf-8'))
print(f'bucket round-trip: this is run #{st["runs"]} (counter survives between runs only if the bucket works)')

if '--call' in sys.argv:
    model, text = gemini.generate('Reply with the single word: ok', ['gemini-3.6-flash', 'gemini-3.8-flash'],
                                  max_tokens=256, timeout=90, task='selftest', units=1)
    print(f'Gemini call: {model} answered {text.strip()[:20]!r}')

if len(labels) < 6:
    sys.exit(f'expected 6 Gemini keys, found {len(labels)} — check the repo secrets')
