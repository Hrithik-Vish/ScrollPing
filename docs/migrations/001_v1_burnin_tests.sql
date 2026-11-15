-- =====================================================================================
-- ScrollPing V1 - section 5.5 SQL tests
--
-- HOW TO RUN: paste ONE block at a time into the Supabase SQL editor and press Run.
-- Blocks are separated by lines of "=====". The editor only shows the result of the LAST
-- statement, so every block ends with a SELECT you can read (and paste back to me).
--
-- WHAT THESE TOUCH: only your real novel / user / website rows you pick in block 1, and every
-- change to them is put back by the block itself (inside one transaction: if a block errors,
-- Postgres rolls the whole block back). No Telegram messages are sent. Test queue rows use
-- chapter numbers >= 90000 so they are easy to recognise, and block 7 deletes them.
-- =====================================================================================


-- =====================================================================================
-- BLOCK 1 - FIND IDS. Run this, pick ONE row, copy its three ids into block 2.
-- (needs an ACTIVE user who is 'reading' that novel; the user can be your own test account)
-- =====================================================================================
select s.user_id, s.novel_id, n.web_id,
       n.novel_name, w.web_domain_name, u.username, u.status as user_status
  from public.user_subscriptions s
  join public.users    u on u.id = s.user_id
  join public.novels   n on n.id = s.novel_id
  join public.websites w on w.id = n.web_id
 where s.reading_status = 'reading' and u.status = 'active'
 order by n.updated_at desc
 limit 10;


-- =====================================================================================
-- BLOCK 2 - SETUP. Replace the three <...> values (keep the quotes), then Run.
-- Creates two scratch tables + two helper functions (all named _t55_*, removed in block 7)
-- and shows a preconditions table. Every row must say ok = true before you continue.
-- =====================================================================================
create table if not exists public._t55_results (n bigserial primary key, step text, ok boolean, detail text);
create table if not exists public._t55_params  (k text primary key, v text);
alter table public._t55_results enable row level security;
alter table public._t55_params  enable row level security;
truncate public._t55_results, public._t55_params;

insert into public._t55_params(k, v) values
  ('novel_id', '<NOVEL_ID>'),
  ('user_id',  '<USER_ID>'),
  ('web_id',   '<WEB_ID>');

create or replace function public._t55_p(p_key text) returns uuid
language sql stable as $$ select v::uuid from public._t55_params where k = p_key $$;

create or replace function public._t55_rec(p_step text, p_ok boolean, p_detail text default null) returns void
language sql as $$ insert into public._t55_results(step, ok, detail) values (p_step, coalesce(p_ok, false), p_detail) $$;

select 'novel exists and belongs to that website' as precondition,
       exists (select 1 from public.novels where id = public._t55_p('novel_id') and web_id = public._t55_p('web_id')) as ok
union all
select 'user is active',
       exists (select 1 from public.users where id = public._t55_p('user_id') and status = 'active')
union all
select 'user is reading that novel',
       exists (select 1 from public.user_subscriptions
                where user_id = public._t55_p('user_id') and novel_id = public._t55_p('novel_id') and reading_status = 'reading')
union all
select 'queue holds no real pending/sending rows (tests claim from the whole queue)',
       not exists (select 1 from public.pending_notifications
                    where status in ('pending','sending') and chapter < 90000)
union all
select 'no leftover test rows from an earlier attempt',
       not exists (select 1 from public.pending_notifications where chapter >= 90000);


-- =====================================================================================
-- BLOCK 3 - NOVEL FUNCTIONS (update_novels_batch, sync_novel_titles). Run, read the table.
-- Temporarily edits the novel you chose, then restores latest_chap, chapter_url,
-- display_title and updated_at to exactly what they were.
-- =====================================================================================
DO $$
DECLARE
  v_novel uuid := public._t55_p('novel_id');
  o record; a record;
  v_ids uuid[]; v_n int;
  v_old timestamptz := now() - interval '5 days';
BEGIN
  SELECT * INTO o FROM public.novels WHERE id = v_novel;

  -- 1: batch update writes chapter + url, keeps title when none sent, refreshes updated_at
  BEGIN
    UPDATE public.novels SET updated_at = v_old WHERE id = v_novel;
    SELECT array_agg(updated_id) INTO v_ids FROM public.update_novels_batch(
      jsonb_build_array(jsonb_build_object('id', v_novel, 'latest_chap', 1, 'chapter_url', 'https://t55.example/c1', 'display_title', null)));
    SELECT * INTO a FROM public.novels WHERE id = v_novel;
    PERFORM public._t55_rec('3.1 update_novels_batch returns the updated id', v_ids = ARRAY[v_novel], v_ids::text);
    PERFORM public._t55_rec('3.1 latest_chap and chapter_url written', a.latest_chap = 1 AND a.chapter_url = 'https://t55.example/c1', format('%s | %s', a.latest_chap, a.chapter_url));
    PERFORM public._t55_rec('3.1 display_title kept when none is sent', a.display_title IS NOT DISTINCT FROM o.display_title, coalesce(a.display_title, '(null)'));
    PERFORM public._t55_rec('3.1 updated_at refreshed by a chapter change', a.updated_at > v_old, a.updated_at::text);
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('3.1 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 2: a title sent with a chapter update is stored; whitespace-only counts as none
  BEGIN
    PERFORM public.update_novels_batch(jsonb_build_array(jsonb_build_object('id', v_novel, 'latest_chap', 2, 'chapter_url', 'https://t55.example/c2', 'display_title', '  T55 Title  ')));
    SELECT * INTO a FROM public.novels WHERE id = v_novel;
    PERFORM public._t55_rec('3.2 display_title stored (trimmed)', a.display_title = 'T55 Title', a.display_title);
    PERFORM public._t55_rec('3.2 whitespace-only title does not erase it', (
      SELECT count(*) FROM public.update_novels_batch(jsonb_build_array(jsonb_build_object('id', v_novel, 'latest_chap', 3, 'chapter_url', 'https://t55.example/c3', 'display_title', '   ')))) = 1
      AND (SELECT display_title FROM public.novels WHERE id = v_novel) = 'T55 Title', NULL);
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('3.2 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 3: a batch containing an unknown id: only the real one comes back (callers treat a missing id as 'failed')
  BEGIN
    SELECT array_agg(updated_id) INTO v_ids FROM public.update_novels_batch(jsonb_build_array(
      jsonb_build_object('id', v_novel, 'latest_chap', 4, 'chapter_url', 'https://t55.example/c4'),
      jsonb_build_object('id', gen_random_uuid(), 'latest_chap', 4, 'chapter_url', 'https://x')));
    PERFORM public._t55_rec('3.3 unknown ids are simply absent from the result', v_ids = ARRAY[v_novel], v_ids::text);
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('3.3 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 4: an out-of-range chapter makes the whole call fail and writes nothing
  BEGIN
    BEGIN
      PERFORM public.update_novels_batch(jsonb_build_array(jsonb_build_object('id', v_novel, 'latest_chap', 99999999, 'chapter_url', 'https://t55.example/bad')));
      PERFORM public._t55_rec('3.4 out-of-range latest_chap is rejected', false, 'no error was raised');
    EXCEPTION WHEN OTHERS THEN
      PERFORM public._t55_rec('3.4 out-of-range latest_chap is rejected', true, SQLSTATE);
    END;
    PERFORM public._t55_rec('3.4 nothing was written by the failed call', (SELECT chapter_url FROM public.novels WHERE id = v_novel) <> 'https://t55.example/bad', NULL);
  END;

  -- 5: sync_novel_titles changes the title but NOT updated_at (D4), ignores empty titles and no-ops
  BEGIN
    UPDATE public.novels SET display_title = NULL, updated_at = v_old WHERE id = v_novel;
    SELECT array_agg(updated_id) INTO v_ids FROM public.sync_novel_titles(jsonb_build_array(jsonb_build_object('id', v_novel, 'display_title', ' T55 Synced ')));
    SELECT * INTO a FROM public.novels WHERE id = v_novel;
    PERFORM public._t55_rec('3.5 sync_novel_titles returns the id and sets the title', v_ids = ARRAY[v_novel] AND a.display_title = 'T55 Synced', coalesce(a.display_title, '(null)'));
    PERFORM public._t55_rec('3.5 sync_novel_titles leaves updated_at alone (D4)', a.updated_at = v_old, a.updated_at::text);
    SELECT count(*) INTO v_n FROM public.sync_novel_titles(jsonb_build_array(jsonb_build_object('id', v_novel, 'display_title', 'T55 Synced')));
    PERFORM public._t55_rec('3.5 same title again is a no-op (nothing returned)', v_n = 0, v_n::text);
    SELECT count(*) INTO v_n FROM public.sync_novel_titles(jsonb_build_array(jsonb_build_object('id', v_novel, 'display_title', '   ')));
    PERFORM public._t55_rec('3.5 empty title never erases an existing one', v_n = 0 AND (SELECT display_title FROM public.novels WHERE id = v_novel) = 'T55 Synced', NULL);
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('3.5 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- put the real row back exactly as it was
  UPDATE public.novels
     SET latest_chap = o.latest_chap, chapter_url = o.chapter_url, display_title = o.display_title, updated_at = o.updated_at
   WHERE id = v_novel;
  PERFORM public._t55_rec('3.9 novel restored to its original values', (SELECT (n.latest_chap, n.chapter_url, n.display_title, n.updated_at) IS NOT DISTINCT FROM (o.latest_chap, o.chapter_url, o.display_title, o.updated_at) FROM public.novels n WHERE n.id = v_novel), NULL);
END $$;

select n, step, ok, detail from public._t55_results order by n;


-- =====================================================================================
-- BLOCK 4 - QUEUE FUNCTIONS (claim, mark sent/dead, release, maintain, paused user, dedupe).
-- Inserts scratch queue rows (chapters 90001+), deletes them again, and temporarily flips
-- the chosen subscription / user status, restoring both.
-- =====================================================================================
DO $$
DECLARE
  v_user  uuid := public._t55_p('user_id');
  v_novel uuid := public._t55_p('novel_id');
  v_a uuid; v_b uuid; v_c uuid; v_d uuid;
  v_ids uuid[] := ARRAY[]::uuid[]; v_bad int; v_n int; r record;
BEGIN
  -- 4.1 claim: returns exactly the due rows with every joined column filled, flips them to sending
  BEGIN
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, due_at) VALUES (v_user, v_novel, 90001, 'https://t55.example/a', now()) RETURNING id INTO v_a;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, due_at) VALUES (v_user, v_novel, 90002, 'https://t55.example/b', now()) RETURNING id INTO v_b;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, due_at) VALUES (v_user, v_novel, 90003, 'https://t55.example/future', now() + interval '1 hour') RETURNING id INTO v_c;
    v_bad := 0;
    FOR r IN SELECT * FROM public.claim_due_notifications(10) LOOP
      v_ids := v_ids || r.notification_id;
      IF r.telegram_user_id IS NULL OR r.novel_name IS NULL OR r.web_domain_name IS NULL OR r.chapter IS NULL
         OR r.chapter_url IS NULL OR r.access_state IS NULL OR r.due_at IS NULL OR r.attempts IS NULL THEN v_bad := v_bad + 1; END IF;
    END LOOP;
    PERFORM public._t55_rec('4.1 claim returns exactly the two due rows (not the future one)', cardinality(v_ids) = 2 AND v_ids @> ARRAY[v_a, v_b], v_ids::text);
    PERFORM public._t55_rec('4.1 claimed rows carry user, novel, site and access fields', v_bad = 0, v_bad::text);
    SELECT count(*) INTO v_n FROM public.pending_notifications WHERE id IN (v_a, v_b) AND status = 'sending' AND attempts = 1 AND last_attempt_at IS NOT NULL;
    PERFORM public._t55_rec('4.1 claimed rows are sending with attempts = 1', v_n = 2, v_n::text);
    SELECT count(*) INTO v_n FROM public.claim_due_notifications(10);
    PERFORM public._t55_rec('4.1 a second claim returns nothing (no double delivery)', v_n = 0, v_n::text);
    -- 4.2 acknowledgements
    SELECT public.mark_notifications_sent(ARRAY[v_a]) INTO v_n;
    PERFORM public._t55_rec('4.2 mark_notifications_sent marks a sending row', v_n = 1 AND (SELECT status FROM public.pending_notifications WHERE id = v_a) = 'sent' AND (SELECT sent_at FROM public.pending_notifications WHERE id = v_a) IS NOT NULL, v_n::text);
    SELECT public.mark_notifications_sent(ARRAY[v_a]) INTO v_n;
    PERFORM public._t55_rec('4.2 marking the same row sent again changes nothing', v_n = 0, v_n::text);
    SELECT public.mark_notifications_dead(ARRAY[v_b], 'blocked by user') INTO v_n;
    PERFORM public._t55_rec('4.2 mark_notifications_dead records the reason', v_n = 1 AND (SELECT status::text || ':' || error_detail FROM public.pending_notifications WHERE id = v_b) = 'dead:blocked by user', v_n::text);
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('4.1/4.2 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 4.3 maintain: a worker that died leaves rows in sending -> given back; a fresh sending row is left alone
  BEGIN
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, status, last_attempt_at, attempts) VALUES (v_user, v_novel, 90011, 'https://t55.example/stale', 'sending', now() - interval '1 hour', 1) RETURNING id INTO v_a;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, status, last_attempt_at, attempts) VALUES (v_user, v_novel, 90012, 'https://t55.example/fresh', 'sending', now() - interval '5 minutes', 1) RETURNING id INTO v_b;
    SELECT * INTO r FROM public.maintain_notification_queue();
    PERFORM public._t55_rec('4.3 stale sending row is recovered to pending', (SELECT status FROM public.pending_notifications WHERE id = v_a) = 'pending' AND r.recovered = 1, format('recovered=%s', r.recovered));
    PERFORM public._t55_rec('4.3 recently claimed sending row is left alone', (SELECT status FROM public.pending_notifications WHERE id = v_b) = 'sending', NULL);
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('4.3 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 4.4 release_failed: back to pending, or dead when out of attempts / older than 24h
  BEGIN
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, status, last_attempt_at, attempts) VALUES (v_user, v_novel, 90021, 'https://t55.example/r1', 'sending', now(), 1) RETURNING id INTO v_a;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, status, last_attempt_at, attempts) VALUES (v_user, v_novel, 90022, 'https://t55.example/r2', 'sending', now(), 5) RETURNING id INTO v_b;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, status, last_attempt_at, attempts, due_at) VALUES (v_user, v_novel, 90023, 'https://t55.example/r3', 'sending', now(), 1, now() - interval '25 hours') RETURNING id INTO v_c;
    SELECT public.release_failed_notifications(ARRAY[v_a, v_b, v_c], 'timeout') INTO v_n;
    PERFORM public._t55_rec('4.4 release returns the number of rows it touched', v_n = 3, v_n::text);
    PERFORM public._t55_rec('4.4 first failure goes back to pending for the next run', (SELECT status FROM public.pending_notifications WHERE id = v_a) = 'pending', NULL);
    PERFORM public._t55_rec('4.4 fifth failed attempt becomes dead', (SELECT status FROM public.pending_notifications WHERE id = v_b) = 'dead', NULL);
    PERFORM public._t55_rec('4.4 a row older than 24h becomes dead', (SELECT status FROM public.pending_notifications WHERE id = v_c) = 'dead', NULL);
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('4.4 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 4.5 maintain: expiry (>24h overdue -> dead, 23h overdue stays)
  BEGIN
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, due_at) VALUES (v_user, v_novel, 90031, 'https://t55.example/old', now() - interval '25 hours') RETURNING id INTO v_a;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, due_at) VALUES (v_user, v_novel, 90032, 'https://t55.example/recent', now() - interval '23 hours') RETURNING id INTO v_b;
    SELECT * INTO r FROM public.maintain_notification_queue();
    PERFORM public._t55_rec('4.5 a row overdue by more than 24h is expired to dead', (SELECT status::text || ':' || left(error_detail, 7) FROM public.pending_notifications WHERE id = v_a) = 'dead:expired' AND r.expired = 1, format('expired=%s', r.expired));
    PERFORM public._t55_rec('4.5 a row overdue by 23h is still pending', (SELECT status FROM public.pending_notifications WHERE id = v_b) = 'pending', NULL);
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('4.5 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 4.6 maintain: subscription no longer 'reading' -> pending row cancelled (subscription is restored after)
  BEGIN
    UPDATE public.user_subscriptions SET reading_status = 'left' WHERE user_id = v_user AND novel_id = v_novel;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url) VALUES (v_user, v_novel, 90041, 'https://t55.example/cx') RETURNING id INTO v_a;
    SELECT count(*) INTO v_n FROM public.claim_due_notifications(10) WHERE notification_id = v_a;
    PERFORM public._t55_rec('4.6 claim skips a row whose subscription is no longer reading', v_n = 0, v_n::text);
    SELECT * INTO r FROM public.maintain_notification_queue();
    PERFORM public._t55_rec('4.6 maintain cancels it', (SELECT status::text || ':' || error_detail FROM public.pending_notifications WHERE id = v_a) = 'cancelled:subscription ended' AND r.cancelled = 1, format('cancelled=%s', r.cancelled));
    UPDATE public.user_subscriptions SET reading_status = 'reading' WHERE user_id = v_user AND novel_id = v_novel;
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('4.6 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 4.7 paused user: not claimed, not cancelled (D9); delivered after /start
  BEGIN
    UPDATE public.users SET status = 'paused' WHERE id = v_user;
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url) VALUES (v_user, v_novel, 90051, 'https://t55.example/paused') RETURNING id INTO v_a;
    SELECT count(*) INTO v_n FROM public.claim_due_notifications(10) WHERE notification_id = v_a;
    PERFORM public._t55_rec('4.7 a paused user''s row is not claimed', v_n = 0, v_n::text);
    PERFORM public.maintain_notification_queue();
    PERFORM public._t55_rec('4.7 and it is kept (not cancelled) while paused', (SELECT status FROM public.pending_notifications WHERE id = v_a) = 'pending', NULL);
    UPDATE public.users SET status = 'active' WHERE id = v_user;
    SELECT count(*) INTO v_n FROM public.claim_due_notifications(10) WHERE notification_id = v_a;
    PERFORM public._t55_rec('4.7 once the user is active again it is delivered', v_n = 1, v_n::text);
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('4.7 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 4.8 same (user, novel, chapter) can only be queued once; locked rows carry their unlock time
  BEGIN
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, access_state, unlock_at) VALUES (v_user, v_novel, 90061, 'https://t55.example/dup', 'locked', now() + interval '6 hours');
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url) VALUES (v_user, v_novel, 90061, 'https://t55.example/dup2') ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    PERFORM public._t55_rec('4.8 a duplicate (user, novel, chapter) is ignored', v_n = 0, v_n::text);
    SELECT * INTO r FROM public.claim_due_notifications(10) LIMIT 1;
    PERFORM public._t55_rec('4.8 claim returns access_state and unlock_at for locked rows', r.access_state = 'locked' AND r.unlock_at IS NOT NULL, format('%s / %s', r.access_state, r.unlock_at));
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('4.8 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  PERFORM public._t55_rec('4.9 subscription and user back to reading / active',
    (SELECT reading_status::text FROM public.user_subscriptions WHERE user_id = v_user AND novel_id = v_novel) = 'reading'
    AND (SELECT status::text FROM public.users WHERE id = v_user) = 'active', NULL);
END $$;

select n, step, ok, detail from public._t55_results order by n;


-- =====================================================================================
-- BLOCK 5 - TWO-TAB CONCURRENCY TEST (no double claim). Needs two browser tabs.
--
-- 5a. Run this once to queue two scratch rows:
-- =====================================================================================
INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url)
SELECT public._t55_p('user_id'), public._t55_p('novel_id'), c, 'https://t55.example/conc/' || c
  FROM unnest(ARRAY[90101, 90102]) AS c;
select chapter, status, attempts from public.pending_notifications where chapter in (90101, 90102) order by chapter;

-- =====================================================================================
-- 5b. TAB 1: run this, then IMMEDIATELY switch to tab 2 (it holds its row lock for 25 seconds):
-- =====================================================================================
DO $$
BEGIN
  INSERT INTO public._t55_results(step, ok, detail)
  SELECT 'tab1 claimed', true, notification_id::text FROM public.claim_due_notifications(1);
  PERFORM pg_sleep(25);
END $$;
select n, step, ok, detail from public._t55_results where step like 'tab%' order by n;

-- =====================================================================================
-- 5c. TAB 2: run this while tab 1 is still running (within the 25 seconds):
-- =====================================================================================
INSERT INTO public._t55_results(step, ok, detail)
SELECT 'tab2 claimed', true, notification_id::text FROM public.claim_due_notifications(1);
select n, step, ok, detail from public._t55_results where step like 'tab%' order by n;

-- =====================================================================================
-- 5d. After BOTH tabs have finished, run this. PASS = two different ids, one per tab,
--     and both rows are 'sending' with attempts = 1.
-- =====================================================================================
select r.step, r.detail as claimed_id, p.chapter, p.status, p.attempts
  from public._t55_results r
  left join public.pending_notifications p on p.id::text = r.detail
 where r.step like 'tab%'
 order by r.n;


-- =====================================================================================
-- BLOCK 6 - SITE HEALTH, STATS, RETENTION, CRON. Run, read the table.
-- Temporarily replaces the site_health row of the chosen website and puts it back.
-- =====================================================================================
DO $$
DECLARE
  v_web  uuid := public._t55_p('web_id');
  v_user uuid := public._t55_p('user_id');
  v_novel uuid := public._t55_p('novel_id');
  v_orig jsonb; r record; h record; v_n int;
BEGIN
  -- 6.1 site health counters
  BEGIN
    SELECT to_jsonb(s) INTO v_orig FROM public.site_health s WHERE web_id = v_web;
    DELETE FROM public.site_health WHERE web_id = v_web;

    SELECT * INTO r FROM public.record_site_result(v_web, 'failed', 'boom 1');
    PERFORM public._t55_rec('6.1 first failure counts as 1', r.o_consecutive_failures = 1 AND r.o_failure_alerted = false, r::text);
    PERFORM public.record_site_result(v_web, 'failed', 'boom 2');
    SELECT * INTO r FROM public.record_site_result(v_web, 'failed', 'boom 3');
    SELECT * INTO h FROM public.site_health WHERE web_id = v_web;
    PERFORM public._t55_rec('6.1 three failures in a row count as 3 with failing_since set', r.o_consecutive_failures = 3 AND h.failing_since IS NOT NULL AND h.last_status = 'failed', r::text);

    UPDATE public.site_health SET failure_alerted = true WHERE web_id = v_web;
    SELECT * INTO r FROM public.record_site_result(v_web, 'failed', 'boom 4');
    PERFORM public._t55_rec('6.1 more failures keep failure_alerted (no repeat alert)', r.o_consecutive_failures = 4 AND r.o_failure_alerted = true, r::text);

    SELECT * INTO r FROM public.record_site_result(v_web, 'ok');
    SELECT * INTO h FROM public.site_health WHERE web_id = v_web;
    PERFORM public._t55_rec('6.1 an ok run resets the counter and the alert flag', r.o_consecutive_failures = 0 AND r.o_failure_alerted = false AND h.failing_since IS NULL AND h.last_status = 'ok', r::text);
    PERFORM public._t55_rec('6.1 ...and stamps last_resolved_at / last_ok_at', h.last_resolved_at IS NOT NULL AND h.last_ok_at IS NOT NULL, h.last_resolved_at::text);

    PERFORM public.record_site_result(v_web, 'failed', 'boom 5');
    SELECT * INTO r FROM public.record_site_result(v_web, 'degraded');
    SELECT * INTO h FROM public.site_health WHERE web_id = v_web;
    PERFORM public._t55_rec('6.1 degraded also ends a failure streak', r.o_consecutive_failures = 0 AND h.last_status = 'degraded', r::text);

    BEGIN
      PERFORM public.record_site_result(v_web, 'bogus');
      PERFORM public._t55_rec('6.1 an invalid status is rejected', false, 'no error raised');
    EXCEPTION WHEN OTHERS THEN
      PERFORM public._t55_rec('6.1 an invalid status is rejected', true, SQLERRM);
    END;

    DELETE FROM public.site_health WHERE web_id = v_web;
    IF v_orig IS NOT NULL THEN
      INSERT INTO public.site_health SELECT * FROM jsonb_populate_record(null::public.site_health, v_orig);
    END IF;
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('6.1 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 6.2 stats function answers with one row and no nulls
  BEGIN
    SELECT * INTO r FROM public.queue_stats_24h();
    PERFORM public._t55_rec('6.2 queue_stats_24h returns one complete row', r.sent_24h IS NOT NULL AND r.pending_now IS NOT NULL AND r.sending_now IS NOT NULL, r::text);
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('6.2 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 6.3 retention: sent > 30d and dead/cancelled > 60d are deleted, newer rows stay
  BEGIN
    INSERT INTO public.pending_notifications(user_id, novel_id, chapter, chapter_url, status, sent_at, created_at) VALUES
      (v_user, v_novel, 90211, 'https://t55.example/s-old',   'sent',      now() - interval '31 days', now() - interval '31 days'),
      (v_user, v_novel, 90212, 'https://t55.example/s-new',   'sent',      now() - interval '29 days', now() - interval '29 days'),
      (v_user, v_novel, 90213, 'https://t55.example/d-old',   'dead',      NULL, now() - interval '61 days'),
      (v_user, v_novel, 90214, 'https://t55.example/d-new',   'dead',      NULL, now() - interval '59 days'),
      (v_user, v_novel, 90215, 'https://t55.example/c-old',   'cancelled', NULL, now() - interval '61 days');
    PERFORM public.cleanup_notification_logs();
    SELECT count(*) INTO v_n FROM public.pending_notifications WHERE chapter IN (90211, 90213, 90215);
    PERFORM public._t55_rec('6.3 retention deletes sent > 30d and dead/cancelled > 60d', v_n = 0, v_n::text);
    SELECT count(*) INTO v_n FROM public.pending_notifications WHERE chapter IN (90212, 90214);
    PERFORM public._t55_rec('6.3 retention keeps newer rows', v_n = 2, v_n::text);
    DELETE FROM public.pending_notifications WHERE chapter >= 90000 AND user_id = v_user AND novel_id = v_novel AND chapter NOT IN (90101, 90102);
  EXCEPTION WHEN OTHERS THEN
    PERFORM public._t55_rec('6.3 unexpected error', false, SQLSTATE || ' ' || SQLERRM);
  END;

  -- 6.4 the daily retention job is scheduled
  PERFORM public._t55_rec('6.4 cron job cleanup-notification-logs is scheduled daily at 00:10 UTC',
    EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'cleanup-notification-logs' AND schedule = '10 0 * * *'), NULL);

  -- 6.5 the tables exist, are locked down, and the digest marker row is seeded
  PERFORM public._t55_rec('6.5 RLS is on for all four new tables',
    (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relname IN ('user_site_tiers','pending_notifications','site_health','app_state') AND c.relrowsecurity) = 4, NULL);
  PERFORM public._t55_rec('6.5 anon / authenticated cannot read the queue directly',
    NOT has_table_privilege('anon', 'public.pending_notifications', 'select') AND NOT has_table_privilege('authenticated', 'public.pending_notifications', 'select'), NULL);
  PERFORM public._t55_rec('6.5 anon / authenticated cannot run the functions',
    NOT has_function_privilege('anon', 'public.claim_due_notifications(int)', 'execute')
    AND NOT has_function_privilege('authenticated', 'public.claim_due_notifications(int)', 'execute'), NULL);
  PERFORM public._t55_rec('6.5 service_role can run the functions',
    has_function_privilege('service_role', 'public.claim_due_notifications(int)', 'execute'), NULL);
  PERFORM public._t55_rec('6.5 app_state exists (digest marker table)', to_regclass('public.app_state') IS NOT NULL, NULL);
END $$;

select n, step, ok, detail from public._t55_results order by n;


-- =====================================================================================
-- BLOCK 7 - FINAL RESULT + CLEANUP.
-- 7a. Run this and SEND ME THE OUTPUT (all rows):
-- =====================================================================================
select n, step, ok, detail from public._t55_results order by n;

-- =====================================================================================
-- 7b. After I have seen the output, run this to remove every scratch row, table and function.
--     It ends with a check that shows 0 / 0 / 0.
-- =====================================================================================
delete from public.pending_notifications
 where chapter >= 90000 and user_id = public._t55_p('user_id') and novel_id = public._t55_p('novel_id');
drop function if exists public._t55_rec(text, boolean, text);
drop function if exists public._t55_p(text);
drop table if exists public._t55_results;
drop table if exists public._t55_params;
select (select count(*) from public.pending_notifications where chapter >= 90000) as leftover_test_rows,
       (select count(*) from pg_class where relname like '\_t55\_%') as leftover_tables,
       (select count(*) from pg_proc where proname like '\_t55\_%') as leftover_functions;
