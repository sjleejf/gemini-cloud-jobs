# -*- coding: utf-8 -*-
"""check_public.py — refuse to publish anything that must not be public. Run before every push
(and the selftest workflow runs it on every push, so a slip fails loudly on GitHub too).

Fails on: a tracked file that looks like a credential (Gemini / Supabase / GitHub / Anthropic key
shapes, .env), a tracked file under any jobs/*/data/ folder, or a tracked file over 300 KB (real job
data — Bible text, LEB, sermon material — belongs in the private bucket, see cloudstate.py).
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = re.compile(r'AIza[0-9A-Za-z_\-]{30}|AQ\.Ab[0-9A-Za-z_\-]{30}|eyJhbGciOi[0-9A-Za-z_\-]{20}'
                    r'|github_pat_[0-9A-Za-z_]{20}|ghp_[0-9A-Za-z]{30}|sk-ant-[0-9A-Za-z_\-]{20}')
MAX_BYTES = 300_000


def main():
    files = subprocess.run(['git', 'ls-files', '-z'], cwd=HERE, capture_output=True, check=True
                           ).stdout.decode('utf-8').split('\0')
    bad = []
    for f in filter(None, files):
        p = os.path.join(HERE, f)
        if os.path.basename(f) in ('.env',) or re.search(r'(^|/)jobs/[^/]+/data/', f):
            bad.append(f'{f}: job data / env file must not be tracked')
            continue
        if not os.path.isfile(p):
            continue
        if os.path.getsize(p) > MAX_BYTES:
            bad.append(f'{f}: {os.path.getsize(p):,} bytes — data belongs in the private bucket')
        if f != 'check_public.py' and SECRET.search(open(p, encoding='utf-8', errors='ignore').read()):
            bad.append(f'{f}: looks like it contains a credential')
    if bad:
        print('NOT SAFE TO PUBLISH:\n  ' + '\n  '.join(bad))
        sys.exit(1)
    print(f'check_public: {len([f for f in files if f])} tracked files, nothing private')


if __name__ == '__main__':
    main()
