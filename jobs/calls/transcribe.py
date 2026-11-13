# -*- coding: utf-8 -*-
"""transcribe.py — call audio → transcript with who-said-what, through Gemini (used by run.py and by the PC test).

One request carries several calls (packed by audio length, not one call per request: the free tier counts requests).
The owner's voice sample goes first, so Gemini can tell which speaker is 나 (이상준 목사); anyone it cannot place
is marked `?` and the PC indexer then refuses to pin those words on anybody.
"""
import base64
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
import gemini  # noqa: E402

MODELS = ['gemini-3.6-flash', 'gemini-3.8-flash']          # owner, 2026-11-02: 3.6 first for long answers
MP3 = ['-vn', '-ac', '1', '-ar', '16000', '-b:a', '24k']   # speech only: 1 h ≈ 11 MB, which fits one request


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True)


def to_mp3(src, dst):
    run(['ffmpeg', '-y', '-loglevel', 'error', '-i', src, *MP3, dst])
    return dst


def duration(path):
    out = run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path]).stdout
    return float(out.decode().strip() or 0)


PIECE = 600          # a call longer than 15 min is cut into 10-min pieces: in the 2026-11-13 test a 30-min call went
LONG = 900           # wrong about who spoke in its last 5 minutes (9 of 31 lines right; before that 90 %)


def split_mp3(mp3, dur):
    """[(path, offset seconds)] — the file itself when short, else 10-minute pieces."""
    if dur <= LONG:
        return [(mp3, 0)]
    out, off, k = [], 0, 0
    while off < dur - 5:
        p = f'{mp3}.part{k}.mp3'
        run(['ffmpeg', '-y', '-loglevel', 'error', '-ss', str(off), '-t', str(PIECE), '-i', mp3, '-c', 'copy', p])
        out.append((p, off)); off += PIECE; k += 1
    return out


def _sec(t):
    p = [int(x) for x in t.split(':')]
    return p[0] * 60 + p[1] if len(p) == 2 else p[0] * 3600 + p[1] * 60 + p[2]


def stitch(results, offsets):
    """The per-piece results of one call → one result, stamps moved by each piece's offset."""
    lines, title, silent = [], None, True
    for r, off in zip(results, offsets):
        if r is None:
            return None                                         # a piece is missing: the call is not done
        title = title or r['title']
        silent = silent and r['silent']
        for t, w, x in r['lines']:
            s = _sec(t) + off
            lines.append((f'{s // 60:02d}:{s % 60:02d}', w, x))
    return {'title': title, 'lines': lines, 'silent': silent and not lines}


def _b64(path):
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode()


INTRO = (
    "너는 전화 통화 녹음을 받아 적는 사람이다. 통화하는 두 사람 중 한 명은 항상 ‘나’, 곧 이상준 목사(한국어를 쓰는 "
    "개혁장로교 목사)다. 먼저 ‘나’의 목소리 견본을 들려주고, 이어서 통화 녹음 여러 개를 번호 순서로 들려준다. "
    "통화마다 견본과 같은 목소리가 ‘나’다.")

INSTR = (
    "\n\n각 통화를 아래 형식으로 받아 적어라. 통화 사이에는 `=== 통화 N ===` 한 줄(N은 위에서 붙인 번호).\n"
    "=== 통화 N ===\n제목: <통화 내용을 한 줄(25자 안팎)로 요약한 한국어 제목>\n"
    "[mm:ss] 나: <말>\n[mm:ss] 상대: <말>\n\n"
    "규칙:\n"
    "1. ‘나’는 견본과 같은 목소리, 그 밖의 사람은 `상대`(상대가 둘 이상이면 `상대2`). 누구의 목소리인지 정말 "
    "구별할 수 없는 말은 `?`로 쓴다(추측으로 ‘나’나 ‘상대’를 붙이지 마라).\n"
    "2. [mm:ss]는 그 통화가 시작된 때부터의 시각이다. 같은 사람이 이어 말하면 한 줄로 합치되, 한 줄이 300자를 "
    "넘으면 나눈다.\n"
    "3. 들리는 대로 정확히 받아 적는다. 이름·숫자·전화번호·고유명사는 특히 정확히. 알아듣지 못한 곳은 [불명]으로 "
    "두고 지어내지 않는다. 의미 없는 ‘음’, ‘어’만 있는 말은 적지 않는다.\n"
    "4. 자동응답(ARS) 안내 방송·대기 음악·통화 연결음은 `상대: (안내 음성) …`처럼 한두 줄로 줄이고, 목소리가 거의 "
    "없는 통화는 제목 아래에 `(음성 없음)` 한 줄만 쓴다.\n"
    "5. 말을 한국어로 번역하지 말고 들린 언어 그대로 쓴다. 설명·머리말·맺음말은 쓰지 않는다.")


def build_parts(items, ref_mp3=None):
    """items: [(counterpart name, mp3 path)] → Gemini parts."""
    parts = [{'text': INTRO}]
    if ref_mp3:
        parts += [{'text': '[‘나’(이상준 목사)의 목소리 견본]'},
                  {'inline_data': {'mime_type': 'audio/mp3', 'data': _b64(ref_mp3)}}]
    for i, (name, path) in enumerate(items, 1):
        parts += [{'text': f'[통화 {i} — 상대(연락처 이름): {name}]'},
                  {'inline_data': {'mime_type': 'audio/mp3', 'data': _b64(path)}}]
    parts.append({'text': INSTR})
    return parts


_SPLIT = re.compile(r'^=+\s*통화\s*(\d+)\s*=+\s*$', re.M)
_TITLE = re.compile(r'^제목\s*[:：]\s*(.+)$')
_LINE = re.compile(r'^\[?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]?\s*(나|상대\d*|\?)\s*[:：]\s*(.*)$')   # long answers sometimes lose the "["


def parse_answer(text):
    """→ {N: {'title': str|None, 'lines': [(mm:ss, 'me'|'other'|'other2'|'?', text)], 'silent': bool}}"""
    out = {}
    pieces = _SPLIT.split(text)
    for k in range(1, len(pieces) - 1, 2):
        n, body = int(pieces[k]), pieces[k + 1]
        title, lines, silent = None, [], False
        for raw in body.splitlines():
            l = raw.strip()
            if not l:
                continue
            m = _TITLE.match(l)
            if m and title is None and not lines:
                title = m[1].strip(); continue
            m = _LINE.match(l)
            if m:
                who = 'me' if m[2] == '나' else '?' if m[2] == '?' else 'other' + m[2][2:]
                lines.append((m[1], who, m[3].strip()))
            elif '(음성 없음)' in l:
                silent = True
            elif lines:                                       # a continuation line
                t, w, x = lines[-1]
                lines[-1] = (t, w, x + ' ' + l)
        out[n] = {'title': title, 'lines': lines, 'silent': silent}
    return out


def to_samsung_layout(result, counterpart):
    """The transcript file text, in the layout the phone's single export uses (src_calls.py reads it)."""
    def label(w):
        if w == 'me':
            return '나'
        if w == '?':
            return '화자 불명'
        return counterpart if w == 'other' else counterpart + ' ' + w[5:]
    out = ['AI로 생성된 콘텐츠입니다', '']
    if result.get('title'):
        out += [result['title'], '']
    for t, w, x in result['lines']:
        out += [f'{label(w)}  ({t})', x, '']
    return '\n'.join(out)


def transcribe(items, ref_mp3=None, task='sjindex_calls'):
    """items [(name, mp3)] → ({N: result}, model). One Gemini request."""
    parts = build_parts(items, ref_mp3)
    model, text = gemini.generate(None, MODELS, parts=parts, max_tokens=60000, timeout=900, task=task,
                                  units=len(items))
    return parse_answer(text), model, text
