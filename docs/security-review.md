# Security review brief

A self-contained briefing for a security review of **ScrollPing**. It is written so it can be handed to a fresh chat (or a human reviewer) together with the project zip, and it also lives in the project's `docs/` folder.

- **Owner:** Hrithik, a student; a personal project, hand-written Python, one developer.
- **Review date target:** after the burn-in stage, before the project is opened to other people.
- **What the owner wants back:** a prioritised findings report, small fixes that fit the project's constraints, and a draft privacy note (details in "What to deliver").

---

## 1. How to use this document

**Owner:** start a new chat, attach (a) this file and (b) a zip of the repository **without `.env` and without `.git`** (secret scanning of the git history is done locally, see 5.2). Paste the output of the SQL queries in section 5.3. Then paste the "Reviewer instructions" in section 6 as the first message.

**Reviewer:** read sections 2 to 4 for context, then work through the checklist in section 7. Section 8 lists problems the owner already knows about; confirm them, rate them, and propose fixes, but do not stop there: the point of the review is to find what the owner does *not* know.

---

## 2. What ScrollPing is

Watches manga, manhwa and novel aggregator sites for new chapters and sends Telegram alerts to the people following those titles. People sign up and manage their list through a Telegram bot. A web app (the main control hub, with an admin view for the owner) is **planned but not built**; its future needs affect the database policies (see 8, K1 to K4).

### Two programs, one database

| Program | Entry point | Runs on | Started by | Credentials it holds |
|---|---|---|---|---|
| **Scraper** | `main.py` | GitHub Actions (`.github/workflows/scraper.yml`), public repository | cron-job.org (GitHub API `workflow_dispatch`), GitHub's own schedule as backup | `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `TELEGRAM_BOT_TOKEN` (repository secrets); a dummy `SUPABASE_PUBLISHABLE_KEY` |
| **Bot** | `python -m src.bot` | Render free web service (`render.yaml`) | always on (long polling); kept awake by cron-job.org requesting `/health` every 5 minutes | the same four variables as Render environment variables |

Other moving parts: **Supabase** (Postgres, Auth, pg_cron; free tier), **Telegram Bot API**, **cron-job.org** (holds a fine-grained GitHub token), **GitHub Actions CI** (`tests.yml`, runs on push and pull request, uses no secrets).

### Repository map
```
main.py                 scraper pipeline (fetch -> parse -> compare -> update DB -> notify)
site_parse_rules.json   per-site rules: mode, regexes, base_url, link patterns
src/fetching.py         fetch_html (requests, browser-like User-Agent, timeouts)
src/parsing.py          parse_html / parse_html_li_block (BeautifulSoup, lxml, regex)
src/database.py         ALL Supabase access (scraper functions + bot functions)
src/telegramMessenger.py  outgoing alerts (HTML parse mode, html.escape on names and URLs)
src/bot.py              Telegram command handler (python-telegram-bot, long polling)
src/novel_links.py      pasted link -> (site, slug); pure, never fetches
src/ratelimiter.py      per-user token bucket for incoming messages
src/health.py           /health endpoint (stdlib http.server) for Render and the keep-alive timer
render.yaml, .python-version, .github/workflows/*.yml, tests/, docs/
```
`docs/schema.md` has the full SQL, RLS policies and cron jobs; `docs/deployment.md` has the hosting setup; `docs/bot.md` describes the bot's protections.

### Data stored
`users` (Supabase auth user id, Telegram username or first name truncated to 64 characters, `telegram_user_id`, status active or paused), `user_subscriptions` (which novels, reading status), `novels` (slug per site, latest chapter number, chapter URL), `websites`, `archive`, `scraper_logs`. Auth users are created by the bot with a **placeholder email** `tg<telegram_id>@scrollping.invalid` and **no password**. There is no payment data and no free-text user content except Telegram display names and pasted links.

---

## 3. Assets and trust boundaries

**Worth protecting, most to least:**
1. `SUPABASE_SECRET_KEY` (bypasses RLS: full read and write of everything, auth admin).
2. `TELEGRAM_BOT_TOKEN` (impersonate the bot, read messages sent to it, message every user).
3. The GitHub token on cron-job.org (can start workflow runs; scoped to one repo, Actions permission only).
4. User data: Telegram ids, usernames, who follows what.
5. Availability of alerts and of the bot; the owner's free-tier quotas (Supabase 500 MB, GitHub Actions, Render hours).

**Trust boundaries (where untrusted input enters):**
- Telegram messages and button presses (any Telegram user, including bots and scripts).
- Pasted links in `/add` (attacker-chosen strings; **never fetched**, only parsed).
- HTML and URLs returned by the scraped sites (third parties; could be compromised or hostile).
- Anyone holding the Supabase URL and the publishable (anon) key once the web app ships: they will be public by design, so **RLS must be airtight on its own**.
- The public GitHub repository and its public Actions logs.

---

## 4. Threat model (who we worry about)

| Actor | Capability | What they might try |
|---|---|---|
| Random Telegram user | message the bot freely, create unlimited accounts | flood it, register en masse, forge button data, probe other users' data, abuse `/add` input |
| Anyone with the public Supabase URL and anon key | call the REST and Auth APIs directly | sign up, read or write rows RLS lets them, register someone else's `telegram_user_id`, redirect alerts |
| Hostile or compromised scraped site | control HTML and links we parse | inject links or markup into alerts, break parsing, waste CPU (regex), phish users |
| Repository reader | read code, history and public Actions logs | find secrets, find sensitive data in logs, find exploitable code |
| Attacker with one leaked token | use exactly that token | see what each token can do (blast radius) |
| Supply-chain attacker | publish a malicious dependency or action version | run code in CI or on Render |

Out of scope: nation-state attackers, physical access, attacks on Telegram, Supabase, GitHub or Render themselves.

---

## 5. What to send, and how to gather evidence

### 5.1 The zip
Repository root as is, **excluding** `.env` (git-ignored), `.git/` and `venv/`. Tests run offline with dummy values: `pip install -r requirements.txt && python -m pytest -q` (388 passed, 2 skipped).

### 5.2 Secret scanning of the git history (owner, locally)
Run a scanner (for example `gitleaks detect` or `trufflehog git file://.`) over the full history and share **only the findings, with the secret values redacted**. Also check the public Actions run logs by eye. Known history: one bot token was printed in a pasted log and was revoked and replaced via BotFather; confirm the old one is dead.

### 5.3 Database evidence (owner, in the Supabase SQL editor)
```sql
-- RLS on or off per table
select tablename, rowsecurity from pg_tables where schemaname = 'public';
-- all policies
select tablename, policyname, roles, cmd, qual, with_check from pg_policies where schemaname = 'public';
-- what the API roles can do at the privilege level
select grantee, table_name, privilege_type from information_schema.role_table_grants
 where table_schema = 'public' and grantee in ('anon', 'authenticated') order by 2, 1, 3;
-- functions, extensions, cron jobs
select p.proname, p.prosecdef from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public';
select extname from pg_extension;
select jobname, schedule, command from cron.job;
-- constraints and indexes
select conrelid::regclass as tbl, conname, pg_get_constraintdef(oid) from pg_constraint where connamespace = 'public'::regnamespace;
```
Also run the **Security Advisor** and **Performance Advisor** in the Supabase dashboard and share the findings.

**Dashboard-only settings** (describe or screenshot, no secrets): Authentication → Sign In / Providers (is "Allow new users to sign up" off? which providers are enabled?), email confirmation settings, rate limits, redirect URLs and site URL, JWT expiry, and Project Settings → API (which keys exist; do not paste them).

---

## 6. Reviewer instructions (paste as the first message)

> You are reviewing the attached project (`docs/security-review.md` is its brief). Work through the checklist in section 7 against the actual code, SQL output and settings I provide, and use section 8 as a list of problems I already know about.
>
> Rules:
> 1. **Never ask for, accept or repeat real secrets** (keys, tokens, passwords). If you see one in anything I send, say so and tell me to rotate it. Use placeholders.
> 2. **Verify, don't assume.** Cite the file, function and line for each finding. Mark each as *Confirmed* (you traced it or reproduced it, for example with a test), *Likely* or *Needs info* (say exactly what you need).
> 3. **Look beyond the brief.** The checklist and known issues are a floor, not a ceiling. Anything you find that is not listed is the most valuable output.
> 4. **Respect the constraints:** free tiers only (GitHub Actions, Render free, Supabase free, cron-job.org free, Telegram), Python, one developer who writes the code by hand. Prefer small fixes with a pytest test over rewrites; avoid recommending paid services unless there is no alternative, and say so if so.
> 5. **Rate severity** as Critical (data or credential exposure, or remote abuse today), High (exploitable with modest effort), Medium (needs unusual conditions or limited impact), Low (hardening), Info.
> 6. For anything involving the future web app, treat the publishable (anon) key and the Supabase URL as **public**.
> 7. Do not run or suggest running anything against live production systems without saying so first.

---

## 7. Checklist

Each item says what to check and where. "Known" points to a known issue in section 8.

### A. Secrets and credentials
- A1. No secret in the repository or its history; `.gitignore` and `.dockerignore` cover `.env`; no secret in `render.yaml`, workflows or docs. (`render.yaml` lists variable names only, with `sync: false`.)
- A2. Where each secret lives: GitHub repository secrets, Render environment, local `.env`, cron-job.org. Inventory them and rate the blast radius of each leaking (see section 3). Is anything over-privileged?
- A3. The secret key is used only server-side (scraper, bot), never in anything client-facing. Is it ever logged or put in an error message?
- A4. Logging: `httpx` and `httpcore` are forced to WARNING in `bot.py` because Telegram URLs contain the bot token. Does anything else log URLs, headers, tokens or user ids (`print`, `logger`, tracebacks)? Check `main.py`, `src/database.py`, `src/telegramMessenger.py`, `src/bot.py` and the public Actions logs. (Known K12.)
- A5. Token lifecycle: rotation procedure for each secret, expiry dates (GitHub fine-grained token has an expiry), what happens when one expires (known K6).
- A6. The Supabase client is created at import in `database.py` with all three variables required; check error messages on a missing variable don't echo values.

### B. Supabase: RLS, grants, Auth
- B1. RLS is enabled on every table in `public`; policies are correct for **anon** (should get nothing) and **authenticated** (own rows only). Use the SQL output in 5.3. (Known K1 to K3.)
- B2. Privilege grants for `anon` and `authenticated` are not broader than intended (RLS is the only gate if grants are wide).
- B3. Column-level abuse: can a signed-in user update `users.telegram_user_id`, `users.status` or `users.id` on their own row? (Known K2.)
- B4. Public sign-ups: confirm they are disabled; if not, what can an anonymous visitor create or learn (account existence of `tg<id>@scrollping.invalid` emails, sign-in probes)? (Known K4, K7.)
- B5. Placeholder-email auth users with no password: can they be signed into, recovered or abused (magic link, OTP, password reset to an unreachable address, phone/other providers)? Check enabled providers.
- B6. `security definer` functions, views, RPC endpoints, Realtime, Storage: confirm none are exposed unintentionally.
- B7. pg_cron jobs (`docs/schema.md`): run as a privileged role; check each job cannot be influenced by user input and does what is intended. Note: job 2 deletes novels not updated for 97 days, which cascades to delete subscriptions; confirm that is acceptable. Check the null `updated_at` handling and that the bot sets `updated_at` on creation. (Known K17.)
- B8. Data deletion: `/stop` then "Delete my account" deletes the auth user and relies on cascades (`auth.users` to `users` to `user_subscriptions`). Verify nothing personal remains (orphan novels are not personal). (Known K18.)
- B9. Project-level: backups and retention, region, network restrictions, SSL enforcement, who has dashboard access, MFA on the owner's accounts (Supabase, GitHub, Render, cron-job.org, Telegram BotFather).

### C. Bot (`src/bot.py`, `src/database.py` bot functions)
- C1. **Authorization:** every state-changing path uses only `update.effective_user.id` for identity and filters on both the subscription id and the user id (`remove_subscription`, `set_subscription_status`). Try to find any path where callback data, a pasted id or a race lets one user act on another's rows (IDOR).
- C2. **Callback data:** formats `m:`, `st:`, `ss:`, `rm:`, `ry:`, `stop:*`, `no`; parsing and uuid validation; forged or stale data; replay of old buttons (for example an old "delete account" confirmation).
- C3. **Input validation in `/add`:** length cap (300) applied before parsing; `novel_links.parse_novel_link` host and path handling. Fuzz it for host confusion: `user@host`, backslashes, `#` and `?` tricks, unicode and IDN lookalikes, trailing dots, percent-encoding, double slashes, very long input, regex backtracking. The link is never fetched: confirm that no user-supplied string is ever requested (no SSRF).
- C4. **Rate limiting** (`src/ratelimiter.py`, `rate_gate`): token bucket per Telegram id, group chats dropped, warning itself throttled. Bypass by many accounts; memory growth; behaviour on restart. (Known K24.)
- C5. **Registration abuse:** every first `/start` creates an auth user and rows. Many Telegram accounts could fill the free database or auth quota. Is there any global limit? (Known K8.)
- C6. **Cap enforcement:** 50 subscriptions per user is checked in code; races, and a database trigger as the real fix. (Known K5.)
- C7. **Error handling:** the generic error reply leaks nothing; exceptions logged with tracebacks do not contain secrets or other users' data; a failing DB does not lead to wrong behaviour (for example registering an existing user). Bot database functions deliberately raise instead of returning empty results.
- C8. **Telegram specifics:** bot privacy mode and group behaviour; concurrency (handlers run one update at a time by default); `awaiting_link` state per user; Conflict errors from two pollers.
- C9. **Output:** `/list` and menus use plain text (no parse mode); alerts use HTML parse mode with `html.escape` on names and URLs. Is every interpolated value escaped? Are there other places text from the database or sites reaches a message?

### D. Scraper and untrusted web content (`main.py`, `src/fetching.py`, `src/parsing.py`)
- D1. **Chapter URLs:** a stored `chapter_url` is built by `urljoin(base_url, "/" + "/".join(segments))` from path segments of hrefs found in the scraped HTML. The path always starts with a single slash, so the host should not change; confirm by fuzzing (absolute URLs, `//host`, `..`, encoded characters, `javascript:`, very long segments, odd characters in segments such as quotes or angle brackets). (Known K13.)
- D2. **Regex on untrusted input:** `chapter_regex`, `slug_hash_strip_regex` and the parsing keyword regex run on hrefs from hostile pages. Check for catastrophic backtracking; check `re.compile` use and input length limits.
- D3. **Parser robustness:** BeautifulSoup with `lxml` on arbitrary HTML (huge pages, deep nesting, malformed markup); response size limits (`fetch_html` has timeouts `(5, 30)` but is there a size cap?); behaviour on redirects, compression bombs, non-HTML content.
- D4. **Data trust:** values parsed from sites (slugs, chapter numbers) go into the database; check parameterisation (the Supabase client sends JSON, not SQL) and numeric handling (`float()` of attacker-chosen text, `numeric(7,2)` overflow, `NaN`, `inf`). A malicious site could try to push a huge chapter number that blocks all future real updates; consider a sanity bound.
- D5. **Failure isolation:** one hostile or broken site must not stop others or leak into alerts (there is per-site try/except).
- D6. **Scraper etiquette and legal:** the browser-like `User-Agent` spoofs Chrome; check robots.txt and terms of service of the five target sites, request rates, and the owner's exposure. (Known K14.)

### E. CI/CD, hosting and supply chain
- E1. **Workflows:** `tests.yml` triggers on `push` and `pull_request` with read-only permissions and no secrets; `scraper.yml` uses secrets on `workflow_dispatch` and `schedule` only, `permissions: contents: read`. Check for `pull_request_target`, script injection through `${{ ... }}` in `run:` blocks, over-broad permissions, and who can trigger `workflow_dispatch` (anyone with write access, or the token on cron-job.org). Check that Actions logs on a **public** repo expose nothing sensitive.
- E2. **Third-party actions:** `actions/checkout@v7` and `actions/setup-python@v6` are pinned by tag, not commit SHA. Is that acceptable, and what would change if a tag were moved?
- E3. **Python dependencies:** `requirements.txt` pins exact versions (UTF-16 encoded file, which pip handles) without hashes; no Dependabot or equivalent; `pytest` is installed in production; check known vulnerabilities of the pinned versions and the transitive tree. (Known K15.)
- E4. **Render:** environment variable handling, build and runtime logs visibility, the public `*.onrender.com` URL, `/health` behaviour (only `ok` or `bot not running`, no data), `healthCheckPath` restarts, auto-deploy only when CI passes (`autoDeployTrigger: checksPass`), Python version pinned by `.python-version`.
- E5. **Health endpoint** (`src/health.py`, stdlib `ThreadingHTTPServer` bound to `0.0.0.0`): resource exhaustion from many connections, header leakage, accepted methods (GET and HEAD only).
- E6. **Docker dev image** (`Dockerfile`, `.dockerignore`): not used in production; check it does not copy `.env` or run as root unnecessarily, since it is committed.
- E7. **Third-party dependence:** cron-job.org holds a GitHub token; Render's free tier and the keep-alive ping depend on free-tier behaviour. Assess the risks and the fallbacks documented in `docs/deployment.md`.

### F. Privacy and compliance
- F1. Inventory what personal data is stored (section 2) against what is needed (data minimisation): is `username` needed? `photo_url` exists but is unused. Retention of `scraper_logs` (5 days) and everything else.
- F2. Deletion: `/stop` then "Delete my account and data" works end to end (B8). Is there any other copy (logs, backups)?
- F3. **Draft the privacy note** the owner plans to publish: what is collected, why, where it is stored (Supabase region, Render, GitHub logs), who can see it, retention, how to delete it, a contact. Keep it short and plain.
- F4. Telegram-specific: the bot sees only what users send it; confirm nothing else (contacts, phone numbers, location) is requested or stored.

### G. Availability and abuse
- G1. What happens at the free-tier limits (Supabase 500 MB and its inactivity pause, Render's 750 shared hours, Actions minutes) and how the owner would notice.
- G2. Alert pipeline: failed sends are retried within a run only; a notification is not retried in a later run; consider the consequences.
- G3. Poison-pill input: one malformed row or site response must not permanently break a run.

---

## 8. Known open items (the owner knows these; confirm, rate, fix)

| ID | Item | Why it matters |
|---|---|---|
| K1 | `users` insert policy lets any authenticated user write **any** `telegram_user_id` | someone could register another person's Telegram id first (squat) |
| K2 | `users` update policy lets a user change their own `telegram_user_id` and `status` | could redirect their alerts to someone else's chat id (spam a victim), or un-pause themselves |
| K3 | No insert/update policy on `user_subscriptions` or `novels`; `websites` readable only when subscribed | the future web app with the publishable key cannot add novels or list available sites; policies must be designed together with the web app |
| K4 | Public sign-ups may still be enabled in Supabase Auth | anyone with the anon key could create auth users directly (owner should confirm the setting is off; the bot's admin API ignores it) |
| K5 | The 50-novel cap (raised from 20 for the burn-in) is enforced in Python | a database trigger is the real guard |
| K6 | Fine-grained GitHub token on cron-job.org (Actions: read and write, one repository, expires within a year) | third-party custody; expiry silently degrades the scraper to GitHub's late schedule |
| K7 | Placeholder-email auth users with no password | check for sign-in, recovery or enumeration paths through Supabase Auth |
| K8 | No global limit on new registrations | a swarm of Telegram accounts could exhaust free quotas |
| K9 | Bot kept awake by a keep-alive ping on a free plan | availability depends on free-tier rules |
| K10 | The real publishable key is not used by the code today (a dummy is set in CI and Render), but it will ship in the web app | RLS must be airtight even if the key is public |
| K11 | A bot token once appeared in a pasted log and was revoked and replaced | confirm the old token is dead and the new one has never been logged |
| K12 | Public repository, so Actions logs are public | confirm they contain counts only, never ids, usernames or secrets |
| K13 | `chapter_url` comes from hrefs in scraped HTML (host fixed by `base_url`, segments not charset-validated) | check what an attacker-controlled href can inject into alerts (alerts do escape HTML) |
| K14 | Scraping with a spoofed browser User-Agent | robots.txt and terms of service of the target sites |
| K15 | Dependencies pinned but without hashes; no Dependabot; `pytest` in production requirements | supply chain |
| K16 | Actions pinned by tag, not SHA | supply chain |
| K17 | pg_cron deletes novels after 97 days, cascading to subscriptions | silent data loss by design; null `updated_at` interplay |
| K18 | Account deletion relies on cascades from `auth.users` | verify completeness |
| K19 | No privacy note yet | deliverable F3 |
| K24 | Rate limiter is per process and per Telegram account | bypass with many accounts; fine for one process |

---

## 9. Out of scope
The web app (not built; but its policy requirements are part of K3), Telegram's, Supabase's, GitHub's and Render's own infrastructure, physical security, and legal advice beyond flagging obvious terms-of-service concerns.

---

## 10. What to deliver

1. **Findings report**, sorted by severity, each with: ID, title, severity, status (Confirmed, Likely, Needs info), where (file, function, line, or setting), impact, evidence, and a **proposed fix**.
2. **Fix proposals** as small patches or SQL, each with a pytest test for code changes and a verification step for settings and SQL. Group them in a suggested order of work.
3. **A confirmation or refutation of every item in section 8**, with a severity.
4. **Anything found that is not in this document.**
5. **A draft privacy note** (F3).
6. **A short checklist of recurring chores** (token expiry dates, dependency updates, 60-day workflow rule, free-tier watch).

## 11. After the review
The owner brings the findings back to the build chat. Fixes are applied there with tests, the docs (`docs/schema.md`, `docs/deployment.md`, `docs/bot.md`) are updated, and the README status moves to "V1 complete".

## Related notes
[[schema]] · [[deployment]] · [[bot]] · [[database]] · [[main]] · [[pytest]]
