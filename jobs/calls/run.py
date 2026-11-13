# -*- coding: utf-8 -*-
"""calls — phone-call recordings (Dropbox, uploaded by Dropsync) → transcripts with who-said-what (Gemini) → Dropbox.

Runs on GitHub Actions every ~30 min (calls.yml) and exits at once when there is nothing to do or the day's own
budget is spent. Newest calls first, so new calls always go before the back catalogue (owner: "slow and complete,
newest first, over weeks"). Calls are packed by audio length into one request (the free tier counts requests).

State (private bucket, jobs/calls/data/): state.json (done calls, today's request counts), ref.mp3 (the owner's voice
sample, seeded from the PC). The PC indexer (@내 개인 자료 색인 src_calls.py) reads the transcripts from the Dropbox
text folder.

PUBLIC repo = public logs: only counts are printed, never a file name, path or any call text.

Environment: DROPBOX_APP_KEY, DROPBOX_REFRESH_TOKEN, GEMINI_API_KEY_1..6 (+ optional DROPBOX_ARCHIVE, DROPBOX_TEXT,
CALLS_DAY_CAP, CALLS_RUN_MAX, CALLS_BATCH_SEC).
"""
import datetime
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import transcribe as T  # noqa: E402  (puts the repo root on sys.path → gemini)
import dbx  # noqa: E402

gemini = T.gemini
gemini.JOB, gemini.SOURCE = 'calls', '내 개인 자료 색인'
DATA = os.path.join(HERE, 'data')
STATE = os.path.join(DATA, 'state.json')
REF = os.path.join(DATA, 'ref.mp3')

ARCHIVE = os.environ.get('DROPBOX_ARCHIVE', '/Dave Lee/이상준의 개인 파일들/#핸드폰 통화 녹음')
TEXT = os.environ.get('DROPBOX_TEXT', '/Dave Lee/이상준의 개인 파일들/#핸드폰 통화 녹음 텍스트')
CAP = {'old': int(os.environ.get('CALLS_DAY_CAP', '40')),   # Gemini requests per quota day for the back catalogue
       'new': 25}                                           # … and extra ones that fresh calls may use
RUN_MAX = int(os.environ.get('CALLS_RUN_MAX', '6'))         # requests per run
BATCH_SEC = int(os.environ.get('CALLS_BATCH_SEC', '2700'))  # audio per request (45 min)
BATCH_MAX_CALLS = 25
BATCH_MAX_BYTES = 15 * 1024 * 1024                          # one request must stay under 20 MB
FRESH_DAYS = 3
MIN_BYTES = 30000                                           # smaller = no speech
MAX_ATTEMPTS = 3

_NAME = re.compile(r'^통화(?: 녹음)?\s+(?P<who>.+?)_(?P<d>\d{6})_(?P<t>\d{6})')
_OLD = re.compile(r'^(?P<app>phone|kakao|whatsapp|wechat|zoom)_(?P<d>\d{8})-(?P<t>\d{6})(?:_(?P<who>.+?))?\.amr$', re.I)
_APP = {'phone': '', 'kakao': '카톡 ', 'whatsapp': 'WhatsApp ', 'wechat': 'WeChat ', 'zoom': 'Zoom '}   # the 2021 phone's names
AUDIO = ('.m4a', '.amr', '.mp3', '.wav', '.aac', '.ogg')


def say(msg):
    print(msg, flush=True)


def call_info(name):
    """(call key, date YYYY-MM-DD, HHMMSS, who, transcript file name) or None — from the recording's file name."""
    if not name.lower().endswith(AUDIO):
        return None
    m = _NAME.match(name)
    if m:
        d, t, who = m['d'], m['t'], m['who'].strip()
        return f'{who}|{d}_{t}', f'20{d[:2]}-{d[2:4]}-{d[4:]}', t, who, name.rsplit('.', 1)[0] + '_cloud.txt'
    m = _OLD.match(name)                                          # phone_20211228-142149_0314661411.amr
    if m:
        d, t = m['d'][2:], m['t']
        who = (_APP[m['app'].lower()] + (m['who'] or '').strip()).strip() or '번호 모름'
        return f'{who}|{d}_{t}', f'20{d[:2]}-{d[2:4]}-{d[4:]}', t, who, f'통화 {who}_{d}_{t}_cloud.txt'
    return None


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding='utf-8'))
    return {'done': {}, 'tries': {}, 'day': '', 'n': {'old': 0, 'new': 0}}


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    json.dump(st, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False)


def quota_day():
    """The Gemini quota day rolls over at midnight in Los Angeles."""
    from zoneinfo import ZoneInfo
    return datetime.datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d')


def fail(st, key, why):
    """A call that could not be done this time: tried again later, given up after MAX_ATTEMPTS."""
    n = st['tries'].get(key, 0) + 1
    st['tries'][key] = n
    if n >= MAX_ATTEMPTS:
        st['done'][key] = {'s': 'failed', 'why': why}


def other_label(who):
    return '상대' if re.fullmatch(r'[\d\-+ ]+', who) else who      # a bare phone number is no name


def main():
    st = load_state()
    today = quota_day()
    if st.get('day') != today:
        st.update(day=today, n={'old': 0, 'new': 0})
    if time.time() < st.get('rest_until', 0):                    # Gemini was busy last time: rest (every try spends quota)
        say(f'resting after Gemini errors ({st.get("fails", 0)} in a row) — {round((st["rest_until"] - time.time()) / 60)} min left')
        return
    say(f'quota day {today}: back-catalogue requests {st["n"]["old"]}/{CAP["old"]}, fresh {st["n"]["new"]}/{CAP["new"]}')

    box = dbx.Dropbox()
    files = box.list_all(ARCHIVE)
    cand = []
    for f in files:
        info = call_info(f['name'])
        if not info or info[0] in st['done']:
            continue
        if f['size'] < MIN_BYTES:
            st['done'][info[0]] = {'s': 'empty'}
            continue
        cand.append((info[1], info[2], f, info))
    cand.sort(key=lambda c: (c[0], c[1]), reverse=True)          # newest first
    say(f'{len(files)} files in the archive, {len(cand)} calls waiting')
    save_state(st)
    if not cand:
        return
    if not os.path.exists(REF):
        say('no ref.mp3 (the owner voice sample) in the job data — not starting')
        return

    fresh_from = (datetime.date.today() - datetime.timedelta(days=FRESH_DAYS)).isoformat()
    tmp = tempfile.mkdtemp()
    requests, i = 0, 0
    while i < len(cand) and requests < RUN_MAX:
        cat = 'new' if cand[i][0] >= fresh_from else 'old'
        if st['n'][cat] >= CAP[cat]:                              # this kind's allowance for today is spent
            i += 1
            while i < len(cand) and ('new' if cand[i][0] >= fresh_from else 'old') == cat:
                i += 1
            continue
        batch, sec, size = [], 0.0, 0
        while i < len(cand) and len(batch) < BATCH_MAX_CALLS and sec < BATCH_SEC:
            d, _t, f, info = cand[i]
            if ('new' if d >= fresh_from else 'old') != cat:
                break                                             # fresh calls and the back catalogue never share a request
            src = os.path.join(tmp, f"{f['id'].replace(':', '_')}{os.path.splitext(f['name'])[1]}")
            mp3 = src + '.mp3'
            try:
                box.download(f['id'], src)
                T.to_mp3(src, mp3)
                dur = T.duration(mp3)
            except Exception as e:
                say(f'  one call skipped ({type(e).__name__})')
                fail(st, info[0], 'download/convert')
                i += 1
                continue
            finally:
                if os.path.exists(src):
                    os.remove(src)
            if dur < 2:
                st['done'][info[0]] = {'s': 'empty'}; os.remove(mp3); i += 1; continue
            nbytes = os.path.getsize(mp3)
            if nbytes > BATCH_MAX_BYTES:                          # > ~90 min: too big for one request
                fail(st, info[0], 'too long'); st['tries'][info[0]] = MAX_ATTEMPTS; st['done'][info[0]] = {'s': 'failed', 'why': 'too long'}
                os.remove(mp3); i += 1; continue
            if batch and (size + nbytes > BATCH_MAX_BYTES or sec + dur > BATCH_SEC * 1.4):
                os.remove(mp3); break                             # does not fit: the next request starts with it
            batch.append((info, T.split_mp3(mp3, dur), dur)); sec += dur; size += nbytes; i += 1
        if not batch:
            continue
        say(f'request {requests + 1}: {len(batch)} calls, {round(sec / 60)} min of audio ({cat})')
        requests += 1
        st['n'][cat] += 1                                         # counted when sent: a failed request spends quota too
        items, spans, n = [], [], 0                               # items = every piece; spans = which pieces are one call
        for info, pieces, _d in batch:
            first = n + 1
            for k, (p, _off) in enumerate(pieces):
                items.append((info[3] + (f' (이 통화의 {k + 1}/{len(pieces)}번째 부분)' if len(pieces) > 1 else ''), p)); n += 1
            spans.append((first, n))
        fails = st.get('fails', 0)
        try:                                                      # ONE model per try (a 503 spends quota on every model asked);
            result, model, _raw = T.transcribe(items, REF, models=[T.MODELS[fails % 2]])   # 3.6, then 3.8 after a failure
            st['fails'], st['rest_until'] = 0, 0
        except gemini.GeminiError as e:
            fails += 1                                            # not the call's fault: no attempt counted against it
            st['fails'], st['rest_until'] = fails, time.time() + min(14400, 2700 * 2 ** (fails - 1))   # 45 min, 90, 180, 240 …
            say(f'  Gemini did not answer ({str(e)[:60].splitlines()[0]}); resting {round((st["rest_until"] - time.time()) / 60)} min')
            save_state(st)
            break
        written = 0
        for (info, pieces, _dur), (a, b) in zip(batch, spans):
            key, date, _t, who, fname = info
            r = T.stitch([result.get(k) for k in range(a, b + 1)], [off for _p, off in pieces])
            if r is None or (not r['lines'] and not r['silent']):
                fail(st, key, 'no usable answer'); continue
            if r['silent'] or not r['lines']:
                st['done'][key] = {'s': 'silent'}; continue
            text = T.to_samsung_layout(r, other_label(who))
            box.upload(f'{TEXT}/20{date[2:4]}년/{fname}', ('﻿' + text).encode('utf-8'))
            st['done'][key] = {'s': 'ok', 'lines': len(r['lines']), 'model': model}
            written += 1
        for _info, pieces, _d in batch:
            for p, _off in pieces:
                if os.path.exists(p):
                    os.remove(p)
            if pieces and pieces[0][0].endswith('.mp3') and '.part' in pieces[0][0]:
                whole = pieces[0][0].split('.part')[0]
                if os.path.exists(whole):
                    os.remove(whole)
        say(f'  {written} of {len(batch)} transcripts written ({model})')
        save_state(st)
        time.sleep(2)
    save_state(st)


if __name__ == '__main__':
    try:
        main()
    except dbx.DropboxError as e:
        sys.exit(f'Dropbox: {e}')
