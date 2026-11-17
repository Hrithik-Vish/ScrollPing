# Database schema (Supabase / PostgreSQL)

The Supabase project is the source of truth; this note is the SQL and the reasoning behind it, as applied in Nov 2026. Code that uses it: [[database]]. The V1 burn-in additions (queue, tiers, titles, health, SQL functions) are in the last section, and their exact SQL is in [`migrations/001_v1_burnin.sql`](migrations/001_v1_burnin.sql).

## Tables
```sql
create table users (
  id uuid primary key references auth.users (id) on delete cascade,
  username text not null,
  telegram_user_id bigint not null,
  photo_url text,
  created_at timestamptz default now() not null
);
create type user_status as enum ('active', 'paused');
alter table users add column status user_status not null default 'active';
alter table users add constraint users_telegram_user_id_key unique (telegram_user_id);

create table websites (
  id uuid primary key default gen_random_uuid(),
  web_domain_name text not null,
  last_visited_at timestamptz
);
alter table websites add constraint websites_web_domain_name_key unique (web_domain_name);

create table novels (
  id uuid primary key default gen_random_uuid(),
  novel_name text not null,
  web_id uuid references public.websites (id) not null,
  latest_chap numeric(7,2) not null default -1,
  chapter_url text,
  updated_at timestamptz
);
alter table novels add constraint novels_web_id_novel_name_key unique (web_id, novel_name);

create type reading_status as enum ('reading', 'completed', 'left', 'on hiatus');

create table user_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  novel_id uuid not null references public.novels (id) on delete cascade,
  unique (user_id, novel_id),
  reading_status reading_status default 'reading'
);

create table archive (
  id uuid primary key default gen_random_uuid(),
  novel_id uuid not null references public.novels (id) on delete cascade,
  created_at timestamptz default now() not null
);

create type run_status as enum ('success', 'fail');
create table scraper_logs (
  id uuid primary key default gen_random_uuid(),
  last_run_timestamp timestamptz not null default now(),
  run_status run_status not null default 'fail'
);
```

## Why it looks like this
- **`users.id` references `auth.users`.** The future webapp's RLS compares `auth.uid()` with `users.id`. So the bot creates a Supabase auth user (placeholder email, no password) for every Telegram user, see [[database]].`create_user`. Deleting the auth user cascades through `users` to `user_subscriptions`.
- **`users.telegram_user_id` is unique.** Telegram's id is the identity; without the constraint a double `/start` could create two users for one person.
- **`novels (web_id, novel_name)` is unique.** The same slug can exist on two sites, but not twice on one: `fetch_novels` keys by slug per site, and a duplicate would silently shadow the other.
- **`latest_chap` is `not null default -1`.** `-1` means "not scraped yet" (the bot creates novels before the scraper has seen them). It is below every real chapter including 0, so the compare code needs no null handling. The bot must **omit** the column on insert: sending `None` writes a real null (and now fails the `not null`). Anything that displays chapters must treat `-1` as "waiting for first update".
- **`websites.web_domain_name` is unique** because the bot can recreate a website row on demand (the weekly job deletes websites with no novels). The value is exactly the key used in `site_parse_rules.json`.
- **`users.status`** (`active` / `paused`) backs `/stop`: the recipient read and the queue's claim function only consider active users. A user who blocks the bot is paused automatically.
- **`novels.latest_chap`** was an `int` and became `numeric(7,2)` for half-chapters (`66.5`) and mgeko's `26.3` style sub-chapters.

## Row Level Security
RLS is on for every table; the scraper and the bot use the **secret key**, which bypasses it. The policies exist for the future webapp (publishable key, signed-in user):
- `users`: a user can select, update and insert only their own row (`auth.uid() = id`);
- `websites`, `novels`, `archive`: select only rows connected to the user's own subscriptions;
- `user_subscriptions`: select and delete own rows;
- `scraper_logs`: RLS on, no policies, so only the secret key can touch it.

Open items for the security review and the webapp:
- the `users` insert policy lets any signed-in user write **any** `telegram_user_id`, so someone could register another person's Telegram id first;
- there is no insert/update policy on `user_subscriptions` or `novels`, and `websites` is only readable once subscribed, so a webapp using the publishable key cannot add novels or list available sites yet;
- turn off public sign-ups in the Supabase Auth settings (the admin API the bot uses ignores that switch), otherwise anyone holding the publishable key can create auth users directly.

## Cleanup jobs (pg_cron)
| When | Job |
|---|---|
| daily 00:00 | archive novels with `updated_at` older than 90 days |
| daily 00:00 | delete novels with `updated_at` older than 97 days |
| daily 00:00 | delete archive rows older than 7 days when the novel has been updated within 90 days |
| daily 00:00 | delete `scraper_logs` rows older than 5 days |
| Sundays 00:00 | delete novels with no subscribers |
| Sundays 00:05 | delete websites with no novels |

Interactions worth knowing:
- The bot sets `updated_at = now()` when it creates a novel. A null `updated_at` never matches the `< now() - interval` comparisons, so without it a typo'd slug would live forever; with it, dead slugs expire after 97 days.
- A website with no novels is deleted on Sundays, so [[database]].`get_or_create_website` recreates the row when the next user adds a novel.

## V1 burn-in additions
Applied with [`migrations/001_v1_burnin.sql`](migrations/001_v1_burnin.sql); it is safe to run twice. A database that ran the **original** 001 (attempt cap 5) also needs [`migrations/002_raise_attempt_cap.sql`](migrations/002_raise_attempt_cap.sql), which raises the cap to 24 with no downtime; a fresh database gets 24 from 001 directly. Its checks are in [`migrations/001_v1_burnin_tests.sql`](migrations/001_v1_burnin_tests.sql) (run block by block in the SQL editor: 56 checks, including a two-tab test that two workers never claim the same row). Both ran green against the real project before the first live run.

### Enums and columns on existing tables
- Enums: `access_state` (`free`, `locked`, `unknown`), `alert_timing` (`early`, `when_free`), `site_tier` (`standard`, `member`), `delivery_status` (`pending`, `sending`, `sent`, `dead`, `cancelled`).
- `novels.display_title text` (nullable): the scraped title. A title sync never touches `updated_at`, so a title can never keep a dead novel alive past the 97-day cleanup.
- `user_subscriptions.alert_timing alert_timing` (nullable): `null` means "follow my site tier"; set per novel from the bot.

### New tables (all have RLS enabled and **no** policies, and all access is revoked from `anon` and `authenticated`)
- **`user_site_tiers`** `(user_id, web_id, tier)`, unique on `(user_id, web_id)`: whether the user is a standard reader or a member on a site with early access. Members default to being told early, standard readers when a chapter becomes free.
- **`pending_notifications`**, the delivery queue: `user_id`, `novel_id`, `chapter`, `chapter_url`, `access_state`, `unlock_at`, `due_at`, `status`, `attempts`, `last_attempt_at`, `error_detail`, `created_at`, `sent_at`. **Unique on `(user_id, novel_id, chapter)`**, which makes re-queueing a chapter a no-op and is what lets a crash between "queue" and "advance the novel" cost nothing. Indexes cover the due scan and the stale-`sending` scan.
- **`site_health`** `(web_id` primary key`, last_status, consecutive_failures, failure_alerted, failing_since, last_failed_at, last_resolved_at, last_ok_at, last_error)`.
- **`app_state`** `(key, value, updated_at)`: tiny key/value store; today only `last_digest_sent_at`.

### SQL functions
All are `SECURITY DEFINER` with a fixed `search_path`, and executable **only by `service_role`** (revoked from `PUBLIC`, `anon` and `authenticated`; the test script checks this against the real project).

| Function | What it does |
|---|---|
| `update_novels_batch(items jsonb)` | one call updates `latest_chap`, `chapter_url` and `updated_at` for many novels, and `display_title` only when a non-blank one is sent; returns the ids it updated |
| `sync_novel_titles(items jsonb)` | sets titles where they differ; never touches `updated_at`; blank titles never erase one |
| `claim_due_notifications(batch_limit int)` | atomically takes `pending` rows that are due (`due_at <= now`) for **active** users whose subscription is still `reading`, using `FOR UPDATE SKIP LOCKED`; marks them `sending`, counts the attempt, and returns them joined with the Telegram id, novel name and title, and site. Two workers can never receive the same row |
| `mark_notifications_sent(notification_ids uuid[])` | `sending` → `sent`; returns how many changed (0 when repeated) |
| `mark_notifications_dead(notification_ids, err_text)` | `sending` → `dead` with a reason |
| `release_failed_notifications(notification_ids, err_text)` | `sending` → `pending`, or `dead` at the **24-attempt cap** or once the row is 24 hours overdue. The scraper claims a row once per run, so hourly runs give about 24 attempts in the 24 hours a row may live (the cap was 5 originally; databases that ran the original 001 need [`migrations/002_raise_attempt_cap.sql`](migrations/002_raise_attempt_cap.sql)) |
| `maintain_notification_queue()` | returns `recovered` (rows stuck `sending` for over 30 minutes go back to `pending`), `cancelled` (`pending` rows whose subscription no longer exists or is not `reading`; paused users are **kept**, not cancelled) and `expired` (`pending` rows more than 24 hours overdue → `dead`) |
| `record_site_result(p_web_id, p_status, p_error)` | upserts the site's health row: failures extend the streak (and set `failing_since` once), `ok` or `degraded` end it and stamp `last_resolved_at`; an unknown status raises. Returns `o_consecutive_failures`, `o_failure_alerted` |
| `queue_stats_24h()` | one row of last-24-hour numbers for the digest |
| `cleanup_notification_logs()` | retention: `sent` rows older than 30 days and `dead` / `cancelled` rows older than 60 days are deleted |

### Cron job
`cleanup-notification-logs`, daily at 00:10 UTC, runs `cleanup_notification_logs()`.

### Interactions worth knowing
- Everything below cascades: deleting a **user** removes their tier rows and queue rows; deleting a **novel** (the 97-day and no-subscriber jobs) removes its queue rows; deleting a **website** (the Sunday job, when it has no novels) removes its `site_health` row and every user's saved tier for it. The last one means a user who saved an account type and then stopped following everything on that site is simply asked again next time. Keeping websites that still have tier rows would be a small change to that Sunday statement.
- The queue holds at most a few rows per user and chapter, and finished rows are deleted by retention, so it stays far below the free tier's 500 MB.
- Timestamps are `timestamptz`; the Python side compares them in UTC and shows people IST only in admin messages.

## Related notes
[[database]] · [[bot]] · [[main]] · [[scheduling]]
