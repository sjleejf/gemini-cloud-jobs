# -*- coding: utf-8 -*-
"""cloudstate.py — a job's data lives in a PRIVATE Supabase Storage bucket, never in this public repo.

This repository is public so that GitHub Actions minutes are free. Anything committed here can be read
by anyone, so every job keeps its inputs, outputs and progress in jobs/<job>/data/ (gitignored), and
the workflow moves that folder in and out of the private bucket `cloud-jobs` around each run:

    python cloudstate.py pull <job>             cloud-jobs/<job>/data.tar.gz  ->  jobs/<job>/data/
    python cloudstate.py push <job>             jobs/<job>/data/  ->  cloud-jobs/<job>/data.tar.gz
    python cloudstate.py seed <job> <folder>    (on the PC) upload a local folder as the job's data

`push` also moves the run's Gemini usage lines (data/_usage.jsonl, written by gemini.py) onto the end of
cloud-jobs/_usage/cloud.jsonl, which the PC's quota viewer (CapsLock+Q) reads, so cloud calls are
counted against the same daily allowance as the PC's. Runs never overlap (one concurrency group), so
the read-append-write of that file is safe.

Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY: repo secrets in the cloud; on the PC they are read
from the 떠디 project's .env (same Supabase project).
"""
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BUCKET = 'cloud-jobs'
USAGE_OBJ = '_usage/cloud.jsonl'
PC_ENV = r"C:\Users\USER\.claude\@SJ's H∕G 떠디\.env"     # U+2215 in the folder name, not '/'


def _creds():
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not (url and key) and os.path.exists(PC_ENV):
        for line in open(PC_ENV, encoding='utf-8'):
            k, _, v = line.strip().partition('=')
            v = v.strip().strip('"')
            if k == 'SUPABASE_URL' and not url:
                url = v
            elif k == 'SUPABASE_SERVICE_ROLE_KEY' and not key:
                key = v
    if not (url and key):
        sys.exit('cloudstate: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing')
    return url.rstrip('/'), key


def _req(method, path, data=None, ctype='application/octet-stream'):
    url, key = _creds()
    h = {'apikey': key, 'Authorization': 'Bearer ' + key}
    if data is not None:
        h.update({'Content-Type': ctype, 'x-upsert': 'true'})
    r = urllib.request.Request(f'{url}/storage/v1/object/{BUCKET}/{path}', method=method, data=data, headers=h)
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.read()


def get(path):
    """Object bytes, or None if it does not exist yet."""
    try:
        return _req('GET', path)
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):        # Supabase answers 400 "Object not found" for a missing object
            return None
        raise


def put(path, data, ctype='application/octet-stream'):
    _req('POST', path, data, ctype)


def data_dir(job):
    return os.path.join(HERE, 'jobs', job, 'data')


def _pack(folder):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as t:
        for name in sorted(os.listdir(folder)):
            t.add(os.path.join(folder, name), arcname=name)
    return buf.getvalue()


def pull(job):
    d = data_dir(job)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    blob = get(f'{job}/data.tar.gz')
    if blob is None:
        print(f'cloudstate: {job} has no stored data yet — starting empty')
        return
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as t:
        t.extractall(d, filter='data')
    print(f'cloudstate: pulled {job} ({len(blob):,} bytes, {len(os.listdir(d))} entries)')


def _flush_usage(d):
    p = os.path.join(d, '_usage.jsonl')
    if not os.path.exists(p):
        return 0
    new = [ln for ln in open(p, encoding='utf-8').read().splitlines() if ln.strip()]
    if new:
        old = (get(USAGE_OBJ) or b'').decode('utf-8')
        if old and not old.endswith('\n'):
            old += '\n'
        put(USAGE_OBJ, (old + '\n'.join(new) + '\n').encode('utf-8'), 'text/plain')
    os.remove(p)
    return len(new)


def push(job):
    d = data_dir(job)
    if not os.path.isdir(d):
        sys.exit(f'cloudstate: {d} does not exist — nothing to push')
    n = _flush_usage(d)
    blob = _pack(d)
    put(f'{job}/data.tar.gz', blob)
    print(f'cloudstate: pushed {job} ({len(blob):,} bytes){f", {n} usage lines" if n else ""}')


def seed(job, folder):
    blob = _pack(folder)
    put(f'{job}/data.tar.gz', blob)
    print(f'cloudstate: seeded {job} from {folder} ({len(blob):,} bytes)')


if __name__ == '__main__':
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == 'pull':
        pull(a[1])
    elif len(a) == 2 and a[0] == 'push':
        push(a[1])
    elif len(a) == 3 and a[0] == 'seed':
        seed(a[1], a[2])
    else:
        sys.exit(__doc__)
