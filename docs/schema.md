# Database schema (Supabase / PostgreSQL)

The Supabase project is the source of truth; this note is the SQL and the reasoning behind it, as applied in Nov 2026. Code that uses it: [[database]].

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
- **`users.status`** (`active` / `paused`) backs `/stop`: `fetch_subscribers` only returns active users.
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

## Related notes
[[database]] · [[bot]] · [[main]]
