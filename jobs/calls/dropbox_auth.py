# -*- coding: utf-8 -*-
"""dropbox_auth.py — one-time Dropbox sign-in on the PC (OAuth code flow with PKCE: no app secret needed).

    python dropbox_auth.py start <APP_KEY>     opens the browser; click Allow; Dropbox shows a code
    python dropbox_auth.py finish <CODE>       swaps the code for a refresh token and stores it

Stored in C:\\Users\\USER\\SJIndex\\secrets\\ (never synced, never backed up):
    dropbox_app_key.txt, dropbox_refresh_token.txt
The refresh token never prints. It is then copied into the GitHub repo secrets DROPBOX_APP_KEY /
DROPBOX_REFRESH_TOKEN (see README "calls").
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

DIR = os.path.join(os.path.expanduser('~'), 'SJIndex', 'secrets')
PKCE = os.path.join(DIR, 'dropbox_pkce.json')


def start(app_key):
    os.makedirs(DIR, exist_ok=True)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    json.dump({'app_key': app_key, 'verifier': verifier}, open(PKCE, 'w'))
    url = 'https://www.dropbox.com/oauth2/authorize?' + urllib.parse.urlencode({
        'client_id': app_key, 'response_type': 'code', 'code_challenge': challenge,
        'code_challenge_method': 'S256', 'token_access_type': 'offline'})
    print('Open this page, click Allow, and copy the code Dropbox shows:\n' + url)
    webbrowser.open(url)


def finish(code):
    p = json.load(open(PKCE))
    data = urllib.parse.urlencode({'code': code.strip(), 'grant_type': 'authorization_code',
                                   'code_verifier': p['verifier'], 'client_id': p['app_key']}).encode()
    with urllib.request.urlopen(urllib.request.Request('https://api.dropbox.com/oauth2/token', data=data),
                                timeout=60) as r:
        j = json.loads(r.read())
    open(os.path.join(DIR, 'dropbox_app_key.txt'), 'w').write(p['app_key'])
    open(os.path.join(DIR, 'dropbox_refresh_token.txt'), 'w').write(j['refresh_token'])
    os.remove(PKCE)
    print('OK: refresh token stored (not shown). Scopes:', j.get('scope'))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'start':
        start(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == 'finish':
        finish(sys.argv[2])
    else:
        sys.exit(__doc__)
