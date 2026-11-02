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

## Adding a job

1. `jobs/<name>/run.py` (+ `requirements.txt` if it needs packages). Read and write only under
   `jobs/<name>/data/`; call Gemini through `gemini.generate(...)` with `gemini.JOB = '<name>'` and
   `gemini.SOURCE = '<the @project it works for, without the @>'`.
2. Put its starting data in the bucket: `python cloudstate.py seed <name> <local folder>`.
3. `python check_public.py`, push, and start it from the Actions tab (“Run a job”). To make it run by
   itself, add a `schedule:` to `job.yml` (or a copy of it for that job).
