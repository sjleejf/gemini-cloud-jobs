# gemini-cloud-jobs

Background Gemini jobs that run on GitHub Actions. The repository is **public** so that the Actions
minutes are free (a private repository gets only 2,000 a month, which the old jobs used up).

**Nothing private is ever committed here.** Each job keeps its data — inputs, results, progress — in
`jobs/<job>/data/`, which is gitignored and travels to and from a *private* Supabase Storage bucket
around every run (`cloudstate.py`). API keys and the database key are repository secrets.
`check_public.py` fails the build if a credential, a data folder or a large file is ever tracked.

## Pieces

| file | what |
|---|---|
| `gemini.py` | Gemini calls over the 6-key pool (`GEMINI_API_KEY_1..6`): rotating start key, 429 → next key, 503 → give the model up at once (a busy answer still spends the day's allowance). Logs every response. |
| `cloudstate.py` | `pull` / `push` a job's data folder from / to the private bucket; `push` also hands the run's usage lines to the shared cloud usage log. `seed` uploads a local folder as a job's data. |
| `check_public.py` | the publish guard. |
| `jobs/selftest/` | proves keys, storage and the guard work; spends no quota unless started with “call”. |
| `.github/workflows/selftest.yml` | runs the self-test on every push. |
| `.github/workflows/job.yml` | the generic runner, **manual only** — no job runs on a schedule. |

## Job `calls` (built + pushed 2026-11-13; scheduled; transcribing since 2026-11-14)
**Status 2026-11-16:** works end to end (~120 transcripts). The 403 `"free tier is not available for this project"` comes
only from keys 1 and 6 when asked from the runner (they work from the PC); keys 2–5 work. **Fixed 2026-11-16:** `gemini.py`
now skips a key that gives that 403 and remembers it in `data/_key_skip.json` (re-asked after 7 days), so the run goes on
with the other keys instead of resting. GitHub's cron fires every 4–7 h here, not every 30 min.
Phone-call recordings (Dropbox ← Dropsync Pro on the phone) → Gemini audio → transcripts with who-said-what back in
Dropbox, read by the PC index (`@내 개인 자료 색인\src_calls.py`). `jobs/calls/`: `run.py` (the job), `transcribe.py`
(audio → mp3 → Gemini → Samsung-style transcript text), `dbx.py` (Dropbox API, team-space root header),
`dropbox_auth.py` (one-time PC sign-in, PKCE). Workflow `calls.yml` (every 30 min; the job exits at once when idle).
Secrets needed in addition to the 6 Gemini keys + Supabase: `DROPBOX_APP_KEY`, `DROPBOX_REFRESH_TOKEN`. Job data in the
bucket: `ref.mp3` (the owner's voice sample, `python cloudstate.py seed calls <folder with ref.mp3>`), `state.json`.
Own request budget per Gemini quota day (the PC pacer cannot see this runner): 40 for the back catalogue + 25 for
fresh calls (≤ 3 days old), ≤ 6 per run; calls packed by audio length (≈ 45 min per request). **Public logs: the job
prints counts only — never a file name or call text.** A public repo's schedule is switched off by GitHub after 60 days
without repo activity — re-enable it in the Actions tab (or push anything).

## Job `segcut` (built + switched on 2026-11-24; Gemini cuts the Bible for the 떠디 app, around the clock)
`jobs/segcut/run.py` + workflow `segcut.yml`. The PC (`@SJ's H∕G 떠디\scripts\seg_v2\segcut_queue.py`) puts batches (prompt =
rules + atoms) into the private bucket under `segcut/queue/`; this job answers them one by one with Gemini (3.6-flash all day,
3.8-flash only in its good hours), checks each answer with a copy of `seg_v2.validate`, and stores it in `segcut/answers/`.
The PC task `SegCutIngest` receives the answers, applies them and publishes a book when its last batch is in — the cloud never
touches the app or the database. **Budget (per quota day, every request counts):** 3.6 = 28, 3.8 = 30, both in `config.json`
in the bucket and mirrored in the PC's `@Gemini 관련\cloud_budget.json` so the PC pacer keeps them out of its own share.
**Hydrogen trial (2026-11-26):** `gemini-4-hydrogen` gets the first chance at a batch (20 requests/day, 420 s limit); if it
does not answer or fails the check, the same batch goes on to 3.8-flash (good hours) and then 3.6-flash. A bad trial answer
never counts against the batch. `status.json` keeps `tally` + `recent`; PC report: `@Gemini 관련\hydrogen_report.py`.
Off switch: `"trial": {"enabled": false}` in `segcut/config.json`.
Each run loops ~5 h and starts the next one itself (cron every 3 h only restarts a broken chain). Stop: `segcut_queue.py stop`.
Keys 1 and 6 are refused by Google (HTTP 403 “free tier is not available for this project”) from everywhere, so only keys 2–5
work; `gemini.py` skips them by itself. Logs are public: batch ids and counts only.

## Adding a job

1. `jobs/<name>/run.py` (+ `requirements.txt` if it needs packages). Read and write only under
   `jobs/<name>/data/`; call Gemini through `gemini.generate(...)` with `gemini.JOB = '<name>'` and
   `gemini.SOURCE = '<the @project it works for, without the @>'`.
2. Put its starting data in the bucket: `python cloudstate.py seed <name> <local folder>`.
3. `python check_public.py`, push, and start it from the Actions tab (“Run a job”). To make it run by
   itself, add a `schedule:` to `job.yml` (or a copy of it for that job).
