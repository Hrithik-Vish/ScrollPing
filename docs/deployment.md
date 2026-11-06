# Deployment

How ScrollPing runs in production. Two programs, two homes:

| Program | Home | How it runs |
|---|---|---|
| **Scraper** (`main.py`) | GitHub Actions, `.github/workflows/scraper.yml` | one pass per run, started hourly by cron-job.org (GitHub's own schedule as backup) |
| **Bot** (`src/bot.py`) | a Render free web service, `render.yaml` | always on, long polling, kept awake by an external timer |

CI (`tests.yml`) runs the test suite on every push, independent of both. A small external service (cron-job.org) provides the timers that the free tiers lack: one job starts the scraper, another keeps the bot awake.

## The scraper on GitHub Actions
`.github/workflows/scraper.yml` runs one scrape-and-notify pass of `main.py`. Its triggers:

| Trigger | When | Role |
|---|---|---|
| `workflow_dispatch`, called by **cron-job.org** through GitHub's API | every hour, at minute 5 | **primary** |
| `schedule` (GitHub's own cron) | every hour, at minute 35 (UTC) | **backup** |
| `workflow_dispatch` from the "Run workflow" button | on demand | manual runs and testing |

Why this shape: GitHub's `schedule` trigger is best effort. Runs start late under load, slots get dropped, gaps of an hour or more happen, and in a public repository it is switched off after 60 days without repository activity, silently. A workflow started through the API is a different event type and is not subject to that schedule queue; in practice it starts within seconds. It is not guaranteed (the job still waits for a runner), so GitHub's own schedule stays as a safety net. The run list says which trigger started each run: `scraper (workflow_dispatch)` or `scraper (schedule)`.

Running twice an hour is safe: every run compares against the database and only notifies on a change, so a second run right after the first finds nothing new and never double-sends. A `concurrency` group (`scraper`, never cancelled) makes a slow run delay the next one instead of overlapping it.

- **Secrets** (repository secrets): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`. `database.py` insists on `SUPABASE_PUBLISHABLE_KEY` at import, which the scraper never uses, so the workflow sets a dummy value inline.
- **Failure visibility:** `main.py` exits non-zero when a run fails, so the run goes red and GitHub emails the owner.
- **Cadence:** hourly matches what the IP-viability test was built around and is polite to the sites. It can be tightened later by changing the cron-job.org schedule only.

### Setting up the external trigger
1. **Create a fine-grained personal access token** (GitHub → Settings → Developer settings): resource owner = you, **only this repository**, repository permission **Actions: Read and write**, nothing else, and the longest expiry offered (at most one year). It can start workflow runs but cannot read code or secrets, so a leak is low-impact. Note the expiry date somewhere you will see it.
2. **Test it once from a terminal** before configuring anything else (expect HTTP 204 and an empty body; `-i` shows the status line):
   ```bash
   curl -i -X POST \
     -H "Authorization: Bearer <token>" \
     -H "Accept: application/vnd.github+json" \
     -H "X-GitHub-Api-Version: 2022-11-28" \
     -d '{"ref":"main"}' \
     https://api.github.com/repos/Hrithik-Vish/ScrollPing/actions/workflows/scraper.yml/dispatches
   ```
   A run named `scraper (workflow_dispatch)` should appear in the Actions tab within seconds.
3. **In cron-job.org** create a job with that URL, request method **POST**, the three headers above, request body `{"ref":"main"}`, schedule **every hour at minute 5**, and **notifications on failure** enabled. A successful call returns 204. If GitHub answers 403 about a missing `User-Agent`, add a `User-Agent: scrollping-cron` header. 401/403 later usually means the token expired.
4. Do not delete the backup schedule in `scraper.yml`.

When the token expires the external trigger fails, cron-job.org emails you, and the GitHub schedule keeps the scraper running late but alive until you renew the token. The token is also an item for the security review: it lives on a third-party service.

## The bot on Render
The bot gets messages by long polling (outgoing requests only), so it needs no public URL of its own. A Render **web service**, however, must listen on a port, and on the free plan it **spins down after 15 minutes without inbound requests**, which would stop the bot. So the bot also serves a tiny page, `src/health.py`:

```
GET or HEAD  /  or  /health   ->  200 "ok" while the bot is running, 503 "bot not running" otherwise
```

It listens on `HEALTH_PORT`, or on `PORT`, which Render sets (10000 by default); with neither set (a local run) no port is opened. It serves nothing else and never includes anything about users, counts or secrets. A timer that requests `/health` every 5 minutes does two jobs: it keeps the service awake, and it reports when the bot is down (`post_init` marks the bot running once polling is up; `post_shutdown` clears it, and `healthCheckPath` lets Render restart an unhealthy instance too).

### How it deploys
`render.yaml` is a Render Blueprint: a free Python web service that runs `pip install -r requirements.txt` then `python -m src.bot`, with `/health` as its health check and `autoDeployTrigger: checksPass`, so a push to `main` redeploys only when the repository's CI (`tests.yml`) is green. The Python version comes from `.python-version` (3.12, the version CI tests; Render's own default is newer). Tests, docs and `main.py` are part of the same repo, but only the bot's start command runs. A redeploy restarts the bot, with a short gap.

### One-time setup
1. In Render: **New → Blueprint**, connect the GitHub repository and apply. (Or create a **Web Service** by hand: runtime Python, plan Free, the same build and start commands, health check path `/health`.)
2. Enter the four environment variables when prompted (they are `sync: false`, so never committed): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY` (any value; `database.py` requires it) and `TELEGRAM_BOT_TOKEN`.
3. Watch the deploy logs: the health endpoint should start and polling begin. Open `https://<service-name>.onrender.com/health` (it should say `ok`), then send `/start` to the bot.
4. **Keep it awake:** in cron-job.org create a job that does a GET to that `/health` URL **every 5 minutes** (the sleep timer is 15, so 5 leaves room for a missed ping), and turn on failure notifications. A 503 counts as a failure, so you are emailed when the bot stops.

### Rules of the road
- **Only one bot process may poll at a time.** If the local dev bot runs while the Render service does, Telegram answers `Conflict: terminated by other getUpdates request` and the two keep knocking each other off. Stop the local bot first (or use a second bot token for development). A brief Conflict line in the logs during a Render redeploy, while the old and new instance overlap, is also possible.
- **The free plan has 750 instance hours per month per workspace.** One service awake around the clock uses about 720 to 744 of them, which fits, but *every* free web service in the same workspace shares the budget. If it runs out, Render suspends them all until the next month, the bot included. Do not leave other free web services running in the same workspace (a sleeping one costs little, but a busy one adds up), or put them in another workspace.
- **Rotate secrets in two places** when they change: Render's environment variables (bot) and the GitHub repository secrets (scraper). Changing an env var restarts the service.
- A restart only forgets in-memory state: a half-finished `/add` (the user just sends `/add` again) and the rate limiter's buckets. Everything else lives in the database.
- Log output never contains the bot token: `httpx` request logging is kept at WARNING.
- The first request after a spin-down is slow (the service has to start), which is another reason the 5-minute ping matters. The free instance is small (a fraction of a CPU, 512 MB), which is plenty for this bot.

### Risks and fallbacks
- Keeping a free service awake with a timer leans on free-tier behaviour that Render can change, and some people consider it against the spirit of the free plan. The clean fix is the paid always-on plan, which needs no timer and no `/health` ping (the page can stay; it still gives a monitor something to check).
- If Render ever becomes unsuitable, the bot is one ordinary Python process that needs outbound internet and four environment variables; any always-on host that can run `python -m src.bot` works the same way. (A Hugging Face Space was considered first and dropped when the free tier stopped covering Docker Spaces.)
- Switching the bot from polling to webhooks, so a Telegram message wakes a sleeping service and no timer is needed, is possible but changes tested code and was not chosen for V1.

## Related notes
[[bot]] · [[main]] · [[pytest]] · [[schema]]