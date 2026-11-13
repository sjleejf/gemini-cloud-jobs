# -*- coding: utf-8 -*-
"""dbx.py — the few Dropbox API calls the calls job needs (no SDK; urllib only).

Secrets (repo secrets / PC secrets folder): DROPBOX_APP_KEY, DROPBOX_REFRESH_TOKEN (PKCE, no app secret).
The account is a Dropbox TEAM space ("Aaron Trade Co. Dropbox"), so every call carries the Dropbox-API-Path-Root
header with the team's root namespace id (found through users/get_current_account).
PUBLIC repo → public Actions logs: this module never prints a path or a file name.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.dropboxapi.com/2'
CONTENT = 'https://content.dropboxapi.com/2'


class DropboxError(Exception):
    pass


class Dropbox:
    def __init__(self, app_key=None, refresh_token=None):
        self.app_key = app_key or os.environ['DROPBOX_APP_KEY']
        self.refresh = refresh_token or os.environ['DROPBOX_REFRESH_TOKEN']
        self.token, self.token_until, self.root = None, 0, None
        self._login()

    def _login(self):
        data = urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': self.refresh,
                                       'client_id': self.app_key}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request('https://api.dropbox.com/oauth2/token', data=data),
                                        timeout=60) as r:
                j = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise DropboxError(f'token refresh failed: HTTP {e.code}')
        self.token, self.token_until = j['access_token'], time.time() + j.get('expires_in', 14400) - 120
        acc = self.api('users/get_current_account', None)
        self.root = (acc.get('root_info') or {}).get('root_namespace_id')

    def _headers(self, extra=None):
        if time.time() > self.token_until:
            self._login()
        h = {'Authorization': 'Bearer ' + self.token}
        if self.root:
            h['Dropbox-API-Path-Root'] = json.dumps({'.tag': 'root', 'root': self.root})
        h.update(extra or {})
        return h

    def _send(self, url, headers, data, timeout, tries=3):
        for k in range(tries):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method='POST'),
                                            timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and k < tries - 1:
                    time.sleep(5 * (k + 1)); continue
                raise DropboxError(f'HTTP {e.code} on {url.rsplit("/", 1)[-1]}')
            except Exception as e:
                if k < tries - 1:
                    time.sleep(5); continue
                raise DropboxError(f'{type(e).__name__} on {url.rsplit("/", 1)[-1]}')

    def api(self, endpoint, body, timeout=120):
        data = json.dumps(body).encode() if body is not None else b'null'
        h = self._headers({'Content-Type': 'application/json'} if body is not None else {})
        raw = self._send(f'{API}/{endpoint}', h, data if body is not None else None, timeout)
        return json.loads(raw) if raw else {}

    def list_all(self, path):
        """Every file under `path` (recursive): [{'id', 'name', 'size', 'path'}]."""
        out, res = [], self.api('files/list_folder', {'path': path, 'recursive': True, 'limit': 2000})
        while True:
            out += [{'id': e['id'], 'name': e['name'], 'size': e.get('size', 0), 'path': e['path_lower']}
                    for e in res['entries'] if e['.tag'] == 'file']
            if not res.get('has_more'):
                return out
            res = self.api('files/list_folder/continue', {'cursor': res['cursor']})

    def download(self, file_id, dest):
        arg = json.dumps({'path': file_id})                    # ensure_ascii → header stays ASCII
        raw = self._send(f'{CONTENT}/files/download', self._headers({'Dropbox-API-Arg': arg}), None, 600)
        with open(dest, 'wb') as f:
            f.write(raw)
        return dest

    def upload(self, path, data: bytes):
        arg = json.dumps({'path': path, 'mode': 'overwrite', 'autorename': False, 'mute': True})
        self._send(f'{CONTENT}/files/upload',
                   self._headers({'Dropbox-API-Arg': arg, 'Content-Type': 'application/octet-stream'}), data, 300)

    def exists(self, path):
        try:
            self.api('files/get_metadata', {'path': path})
            return True
        except DropboxError:
            return False
