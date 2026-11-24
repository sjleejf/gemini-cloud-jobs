# -*- coding: utf-8 -*-
"""segcut — Gemini cuts the Bible into short reading pieces for the 떠디 app (@SJ's H∕G 떠디), 24 hours a day.

What it does: takes the next unanswered batch from the queue the PC prepared, sends it to Gemini (3.6-flash all day,
3.8-flash only in its good hours), checks the answer with the same rules the PC applies (`validate_answer`, a copy of
seg_v2.validate), and stores it. The PC (task SegCutIngest, receive-only) picks the answers up, applies them, and
publishes a book when its last batch is in. This job never touches the app, the database or any Bible text of its own:
the queue items (prompt = rules + atoms with glosses) live in the PRIVATE bucket `cloud-jobs`.

Bucket layout (all under  segcut/ ):
  queue/index.json            ordered batches [{id:'Job/b001', words:N}, ...]            (PC writes)
  queue/<Book>/<bNNN>.json    {prompt, atoms:[[atom_id, n_words],...], words}            (PC writes)
  config.json                 budgets / models / enabled switch (see DEFAULTS)           (PC writes)
  redo.json                   {id: epoch} — the PC rejected that answer, cut it again    (PC writes)
  answers/index.json          {done:{id:{model,at}}}                                      (this job writes)
  answers/<Book>/<bNNN>.json  {model, at, text}                                           (this job writes)
  status.json                 today's spend, stuck batches, last run                      (this job writes)
  data.tar.gz                 state.json + _key_skip.json (the standard pull/push of the runner)

Budget: each model has a `daily` number of REQUESTS per quota day (00:00 Los Angeles) — every request counts, a 503
too. The PC pacer (@Gemini 관련\\pacer.py) knows these numbers through cloud_budget.json and keeps them out of its own
background share, so the PC's manual-use reserve stays untouched. Spending follows the clock: allowed = daily × share of
the day gone + 2, so a day's allowance is spread over 24 h instead of used up in the first hour.

PUBLIC repo = public logs: this job prints batch ids, counts and model names only — never a prompt, an answer or Bible text.

Environment: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_API_KEY_1..6 (optional SEGCUT_MAX_REQUESTS=N for a test run).   Exit: a GitHub output `more=true` tells
the workflow to start the next run (the job loops for ~5 h per run, then hands over).
"""
import datetime
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import cloudstate  # noqa: E402
import gemini  # noqa: E402

JOB = 'segcut'
gemini.JOB, gemini.SOURCE = JOB, '떠디(클라우드)'
DATA = cloudstate.data_dir(JOB)
STATE = os.path.join(DATA, 'state.json')
P = JOB + '/'

try:
    from zoneinfo import ZoneInfo
    LA = ZoneInfo('America/Los_Angeles')
    KST = ZoneInfo('Asia/Seoul')
except Exception:                                    # no tzdata: Nov-Mar offsets
    LA = datetime.timezone(datetime.timedelta(hours=-8))
    KST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULTS = {
    'enabled': True,                     # False = stop (the run ends and does not start another)
    'run_minutes': 300,                  # one run loops this long, then hands over (GitHub's job limit is 360)
    'gap_min': 9,                        # minutes between two requests to the SAME model
    'rest_min_503': 25,                  # a busy answer: leave that model alone this long (every try spends allowance)
    'rest_min_other': 12,
    'max_fails': 3,                      # a batch whose answer fails the check this many times is set aside
    'models': {                          # 3.8 answers rarely this month (2-6 % some days), so it only tries in its best hours
        'gemini-3.6-flash': {'daily': 28},
        'gemini-3.8-flash': {'daily': 30, 'kst_hours': [4, 5, 6, 7, 9, 10, 13, 14]},
    },
    'temperature': 0.2,
    'max_output_tokens': 32768,
    'timeout': 240,
}
CUTS = ('S', 'W', 'B')
MAX_PIECE_WORDS = 40                     # = seg_v2.MAX_PIECE_WORDS


def say(msg):
    print(msg, flush=True)


# ── bucket helpers ───────────────────────────────────────────────────────────────────
def jget(path, default=None):
    blob = cloudstate.get(P + path)
    if blob is None:
        return default
    try:
        return json.loads(blob.decode('utf-8'))
    except Exception:
        return default


def jput(path, obj):
    cloudstate.put(P + path, json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json')


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULTS))
    mine = jget('config.json') or {}
    for k, v in mine.items():
        if k == 'models' and isinstance(v, dict):
            cfg['models'] = v
        elif k in cfg:
            cfg[k] = v
    return cfg


def load_state():
    try:
        with open(STATE, encoding='utf-8') as f:
            st = json.load(f)
    except Exception:
        st = {}
    for k, v in (('day', ''), ('spent', {}), ('ok', {}), ('last_ts', {}), ('rest_until', {}), ('parked_until', {}),
                 ('fails', {}), ('stuck', {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    with open(STATE, 'w', encoding='utf-8') as f:
        json.dump(st, f)


def la_day(now):
    return datetime.datetime.fromtimestamp(now, LA).date().isoformat()


def la_midnight_after(now):
    n = datetime.datetime.fromtimestamp(now, LA)
    nxt = (n + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def day_share(now):
    n = datetime.datetime.fromtimestamp(now, LA)
    return (n.hour * 3600 + n.minute * 60 + n.second) / 86400.0


# ── the check on an answer (copy of seg_v2.validate, working from the batch's atom list) ──────────────
def parse_answer(text):
    t = (text or '').strip()
    if t.startswith('```'):
        t = t.split('```', 2)[1]
        t = t[4:] if t.startswith('json') else t
    try:
        return json.loads(t[t.find('{'):t.rfind('}') + 1])
    except Exception:
        return None


def validate_answer(atoms, ans):
    """atoms = [[atom_id, n_words], ...] in order. -> (n_pieces, problem or None)"""
    if not isinstance(ans, dict) or not isinstance(ans.get('pieces'), list):
        return 0, 'no JSON pieces'
    pos = {a[0]: i for i, a in enumerate(atoms)}
    starts = []
    for e in ans['pieces']:
        if not (isinstance(e, list) and len(e) == 2 and e[1] in CUTS):
            return 0, 'bad entry'
        try:
            aid = int(e[0])
        except Exception:
            return 0, 'bad id'
        if aid not in pos:
            return 0, 'id not a target atom'
        if starts and pos[aid] <= starts[-1]:
            return 0, 'id out of order'
        starts.append(pos[aid])
    if not starts or starts[0] != 0:
        return 0, 'first target atom is not a piece start'
    for s, e in zip(starts, starts[1:] + [len(atoms)]):
        if sum(a[1] for a in atoms[s:e]) > MAX_PIECE_WORDS:
            return 0, f'piece over {MAX_PIECE_WORDS} words'
    return len(starts), None


# ── the loop ─────────────────────────────────────────────────────────────────────────
def eligible(model, mcfg, cfg, st, now):
    """None if `model` may take a request now, else the reason."""
    if now < st['parked_until'].get(model, 0):
        return 'every key out of daily quota'
    if now < st['rest_until'].get(model, 0):
        return 'resting after a failure'
    hrs = mcfg.get('kst_hours')
    if hrs and datetime.datetime.fromtimestamp(now, KST).hour not in hrs:
        return 'outside its good hours'
    spent = st['spent'].get(model, 0)
    daily = int(mcfg.get('daily', 0))
    if spent >= daily:
        return 'day budget used'
    if spent >= math.floor(daily * day_share(now)) + 2:
        return 'ahead of the clock'
    if now - st['last_ts'].get(model, 0) < cfg['gap_min'] * 60:
        return 'spacing'
    return None


def usage_count():
    p = os.path.join(DATA, '_usage.jsonl')
    try:
        with open(p, encoding='utf-8') as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except OSError:
        return []


def pending(index, answers, redo, st, cfg):
    done = answers.get('done', {})
    out = []
    for b in index.get('batches', []):
        bid = b['id']
        if st['fails'].get(bid, 0) >= cfg['max_fails']:
            continue
        if bid in done and redo.get(bid, 0) <= done[bid].get('ts', 0):
            continue
        out.append(b)
    return out


def write_status(st, index, answers, cfg, now, note=''):
    done = answers.get('done', {})
    total = len(index.get('batches', []))
    jput('status.json', {
        'updated': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'quota_day': st['day'], 'spent_today': st['spent'], 'answers_ok_today': st['ok'],
        'batches_total': total, 'batches_answered': len([1 for b in index.get('batches', []) if b['id'] in done]),
        'stuck': st['stuck'], 'note': note,
        'budget': {m: v.get('daily') for m, v in cfg['models'].items()},
    })


def main():
    t0 = time.time()
    cloudstate.pull(JOB)
    cfg = load_cfg()
    st = load_state()
    index = jget('queue/index.json') or {'batches': []}
    answers = jget('answers/index.json') or {'done': {}}
    redo = jget('redo.json') or {}
    say(f'segcut: {len(index["batches"])} batches in the queue, {len(answers["done"])} answered, '
        f'models {", ".join(cfg["models"])}')
    more = False
    last_cfg_read = t0
    max_req = int(os.environ.get('SEGCUT_MAX_REQUESTS', '0') or 0)     # test knob: stop after this many Gemini requests
    sent = 0
    while True:
        now = time.time()
        if max_req and sent >= max_req:
            say(f'segcut: test limit of {max_req} request(s) reached')
            break
        if now - t0 > cfg['run_minutes'] * 60:
            more = True
            say('segcut: run time used up — handing over to the next run')
            break
        if now - last_cfg_read > 600:                      # pick up changes (budget, stop switch, new queue) every 10 min
            cfg = load_cfg()
            index = jget('queue/index.json') or index
            redo = jget('redo.json') or redo
            last_cfg_read = now
        if not cfg.get('enabled', True):
            say('segcut: switched off in config.json — stopping')
            break
        today = la_day(now)
        if st['day'] != today:
            st.update(day=today, spent={}, ok={})
            st['rest_until'] = {}
            st['parked_until'] = {}
        todo = pending(index, answers, redo, st, cfg)
        if not todo:
            say('segcut: nothing left to cut (queue empty or every open batch set aside)')
            write_status(st, index, answers, cfg, now, 'queue empty')
            break
        model, mcfg, why = None, None, []
        for m, c in cfg['models'].items():
            r = eligible(m, c, cfg, st, now)
            if r is None:
                # a model with good-hours gating goes first when it is open: its allowance is otherwise unused
                if model is None or c.get('kst_hours'):
                    model, mcfg = m, c
            else:
                why.append(f'{m.replace("gemini-", "").replace("-flash", "")}: {r}')
        if model is None:
            time.sleep(60)
            continue
        b = todo[0]
        item = jget(f'queue/{b["id"]}.json')
        if not item:
            say(f'segcut: {b["id"]} is in the index but its item is missing — set aside')
            st['fails'][b['id']] = cfg['max_fails']
            st['stuck'][b['id']] = 'item missing'
            save_state(st)
            continue
        n0 = len(usage_count())
        st['last_ts'][model] = time.time()
        err = None
        text = None
        try:
            used, text = gemini.generate(item['prompt'], [model], json_mode=True, temperature=cfg['temperature'],
                                         max_tokens=cfg['max_output_tokens'], timeout=cfg['timeout'],
                                         task='segcut', units=item.get('words'))
        except gemini.GeminiError as e:
            err = str(e)
        lines = usage_count()[n0:]
        sent += max(1, len(lines))
        for r in lines:                                    # every request counts, a 503 too
            st['spent'][r['model']] = st['spent'].get(r['model'], 0) + 1
        tag = model.replace('gemini-', '').replace('-flash', '')
        if err is not None:
            if 'every key parked' in err or ('HTTP 429' in err and 'PerDay' in err):
                st['parked_until'][model] = la_midnight_after(now) + 120
                say(f'segcut: {b["id"]} {tag}: every key out of daily quota until the next quota day')
            elif 'HTTP 503' in err:
                st['rest_until'][model] = time.time() + cfg['rest_min_503'] * 60
                say(f'segcut: {b["id"]} {tag}: busy (503) — resting that model {cfg["rest_min_503"]} min')
            elif 'MAX_TOKENS' in err:
                st['fails'][b['id']] = st['fails'].get(b['id'], 0) + 1
                say(f'segcut: {b["id"]} {tag}: answer cut off (MAX_TOKENS), fail {st["fails"][b["id"]]}')
            else:
                st['rest_until'][model] = time.time() + cfg['rest_min_other'] * 60
                say(f'segcut: {b["id"]} {tag}: {err.split(":")[0][:60]} — resting {cfg["rest_min_other"]} min')
        else:
            n, problem = validate_answer(item['atoms'], parse_answer(text))
            if problem:
                st['fails'][b['id']] = st['fails'].get(b['id'], 0) + 1
                say(f'segcut: {b["id"]} {tag}: answer rejected ({problem}), fail {st["fails"][b["id"]]}')
                if st['fails'][b['id']] >= cfg['max_fails']:
                    st['stuck'][b['id']] = problem
            else:
                at = int(time.time())
                jput(f'answers/{b["id"]}.json', {'model': used, 'at': at, 'text': text})
                answers = jget('answers/index.json') or {'done': {}}
                answers['done'][b['id']] = {'model': used, 'ts': at}
                jput('answers/index.json', answers)
                st['ok'][model] = st['ok'].get(model, 0) + 1
                st['fails'].pop(b['id'], None)
                st['stuck'].pop(b['id'], None)
                say(f'segcut: {b["id"]} {tag}: ok, {n} pieces ({item.get("words")} words); '
                    f'{len(answers["done"])}/{len(index["batches"])} answered')
        save_state(st)
        write_status(st, index, answers, cfg, time.time())
        cloudstate.push(JOB)                                # state + usage lines (CapsLock+Q) after every request
        # re-read what the PC may have changed
        redo = jget('redo.json') or redo
    save_state(st)
    cloudstate.push(JOB)
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as f:
            f.write(f'more={"true" if more else "false"}\n')


if __name__ == '__main__':
    main()
