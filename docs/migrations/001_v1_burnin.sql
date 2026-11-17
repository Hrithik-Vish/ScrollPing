-- ============================================================================
-- ScrollPing V1 burn-in migration. Additive only. Safe to re-run.
-- ============================================================================

-- 1. Enums ------------------------------------------------------------------
DO $$ BEGIN CREATE TYPE public.access_state    AS ENUM ('free','locked','unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE public.alert_timing    AS ENUM ('early','when_free');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE public.site_tier       AS ENUM ('standard','member');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN CREATE TYPE public.delivery_status AS ENUM ('pending','sending','sent','dead','cancelled');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 2. Existing tables --------------------------------------------------------
ALTER TABLE public.novels             ADD COLUMN IF NOT EXISTS display_title text;
ALTER TABLE public.user_subscriptions ADD COLUMN IF NOT EXISTS alert_timing  public.alert_timing;  -- NULL = follow site tier

-- 3. user_site_tiers --------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_site_tiers (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES public.users(id)    ON DELETE CASCADE,
    web_id     uuid NOT NULL REFERENCES public.websites(id) ON DELETE CASCADE,
    tier       public.site_tier NOT NULL DEFAULT 'standard',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_site_tiers_user_web_uk UNIQUE (user_id, web_id)
);

-- 4. pending_notifications (the delivery queue) -----------------------------
CREATE TABLE IF NOT EXISTS public.pending_notifications (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES public.users(id)  ON DELETE CASCADE,
    novel_id        uuid NOT NULL REFERENCES public.novels(id) ON DELETE CASCADE,
    chapter         numeric(7,2) NOT NULL,
    chapter_url     text NOT NULL,
    access_state    public.access_state NOT NULL DEFAULT 'free',
    unlock_at       timestamptz,                      -- known unlock moment, NULL if unknown
    due_at          timestamptz NOT NULL DEFAULT now(),
    status          public.delivery_status NOT NULL DEFAULT 'pending',
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_at timestamptz,
    error_detail    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    CONSTRAINT pending_notifications_uk UNIQUE (user_id, novel_id, chapter)
);
CREATE INDEX IF NOT EXISTS idx_pn_due      ON public.pending_notifications (status, due_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_pn_sending  ON public.pending_notifications (status, last_attempt_at) WHERE status = 'sending';
CREATE INDEX IF NOT EXISTS idx_pn_novel    ON public.pending_notifications (novel_id);

-- 5. site_health ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.site_health (
    web_id               uuid PRIMARY KEY REFERENCES public.websites(id) ON DELETE CASCADE,
    last_status          text NOT NULL DEFAULT 'ok' CHECK (last_status IN ('ok','degraded','failed')),
    consecutive_failures integer NOT NULL DEFAULT 0,
    failure_alerted      boolean NOT NULL DEFAULT false,
    failing_since        timestamptz,
    last_failed_at       timestamptz,
    last_resolved_at     timestamptz,
    last_ok_at           timestamptz,
    last_error           text,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- 6. app_state (small key/value state that must not live in a purged table) --
CREATE TABLE IF NOT EXISTS public.app_state (
    key        text PRIMARY KEY,
    value      text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.app_state (key, value) VALUES ('last_digest_sent_at', NULL)
ON CONFLICT (key) DO NOTHING;

-- 7. Functions --------------------------------------------------------------
-- 7.1 Chapter updates. Sets updated_at (your cleanup jobs depend on it).
--     Returns the ids it updated; anything missing from the result failed or no longer exists.
CREATE OR REPLACE FUNCTION public.update_novels_batch(items jsonb)
RETURNS TABLE (updated_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    WITH incoming AS (
        SELECT (e->>'id')::uuid                           AS id,
               (e->>'latest_chap')::numeric(7,2)          AS latest_chap,
               e->>'chapter_url'                          AS chapter_url,
               NULLIF(btrim(e->>'display_title'), '')     AS display_title
          FROM jsonb_array_elements(items) AS e
    )
    UPDATE public.novels AS n
       SET latest_chap   = i.latest_chap,
           chapter_url   = i.chapter_url,
           display_title = COALESCE(i.display_title, n.display_title),
           updated_at    = now()
      FROM incoming AS i
     WHERE n.id = i.id
    RETURNING n.id;
END;
$$;

-- 7.2 Title-only sync. Does NOT touch updated_at. Only writes when the title differs.
CREATE OR REPLACE FUNCTION public.sync_novel_titles(items jsonb)
RETURNS TABLE (updated_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    WITH incoming AS (
        SELECT (e->>'id')::uuid AS id, NULLIF(btrim(e->>'display_title'), '') AS display_title
          FROM jsonb_array_elements(items) AS e
    )
    UPDATE public.novels AS n
       SET display_title = i.display_title
      FROM incoming AS i
     WHERE n.id = i.id
       AND i.display_title IS NOT NULL
       AND n.display_title IS DISTINCT FROM i.display_title
    RETURNING n.id;
END;
$$;

-- 7.3 Queue maintenance. Run at the START of every scraper run.
CREATE OR REPLACE FUNCTION public.maintain_notification_queue(stale_timeout interval DEFAULT interval '30 minutes')
RETURNS TABLE (recovered int, cancelled int, expired int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
DECLARE v_rec int; v_can int; v_exp int;
BEGIN
    -- a) rows left in 'sending' by a run that died: give them back
    UPDATE public.pending_notifications
       SET status = 'pending', error_detail = 'recovered: worker did not finish'
     WHERE status = 'sending' AND last_attempt_at < now() - stale_timeout;
    GET DIAGNOSTICS v_rec = ROW_COUNT;

    -- b) subscription removed, or no longer 'reading': nothing to deliver
    UPDATE public.pending_notifications AS p
       SET status = 'cancelled', error_detail = 'subscription ended'
     WHERE p.status = 'pending'
       AND NOT EXISTS (SELECT 1 FROM public.user_subscriptions s
                        WHERE s.user_id = p.user_id AND s.novel_id = p.novel_id
                          AND s.reading_status = 'reading');
    GET DIAGNOSTICS v_can = ROW_COUNT;

    -- c) too old to be worth sending
    UPDATE public.pending_notifications
       SET status = 'dead', error_detail = 'expired: not delivered within 24h of due time'
     WHERE status = 'pending' AND due_at < now() - interval '24 hours';
    GET DIAGNOSTICS v_exp = ROW_COUNT;

    RETURN QUERY SELECT v_rec, v_can, v_exp;
END;
$$;

-- 7.4 Atomic claim. Flips due rows to 'sending' and returns what to send.
--     Keep batch_limit <= 1000 (PostgREST returns at most 1000 rows per request by default).
CREATE OR REPLACE FUNCTION public.claim_due_notifications(batch_limit int DEFAULT 500)
RETURNS TABLE (
    notification_id uuid, user_id uuid, telegram_user_id bigint, novel_id uuid,
    novel_name text, web_domain_name text, chapter numeric(7,2), chapter_url text,
    display_title text, access_state public.access_state, unlock_at timestamptz,
    due_at timestamptz, attempts int
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
    RETURN QUERY
    WITH picked AS (
        SELECT p.id
          FROM public.pending_notifications p
          JOIN public.user_subscriptions s
            ON s.user_id = p.user_id AND s.novel_id = p.novel_id AND s.reading_status = 'reading'
          JOIN public.users u
            ON u.id = p.user_id AND u.status = 'active'
         WHERE p.status = 'pending' AND p.due_at <= now()
         ORDER BY p.due_at, p.id
         LIMIT batch_limit
         FOR UPDATE OF p SKIP LOCKED
    ), claimed AS (
        UPDATE public.pending_notifications AS p
           SET status = 'sending', last_attempt_at = now(), attempts = p.attempts + 1
          FROM picked
         WHERE p.id = picked.id
        RETURNING p.id AS pid, p.user_id AS puser, p.novel_id AS pnovel, p.chapter AS pchap,
                  p.chapter_url AS purl, p.access_state AS pstate, p.unlock_at AS punlock,
                  p.due_at AS pdue, p.attempts AS pattempts
    )
    SELECT c.pid, c.puser, u.telegram_user_id, c.pnovel, n.novel_name, w.web_domain_name,
           c.pchap, c.purl, n.display_title, c.pstate, c.punlock, c.pdue, c.pattempts
      FROM claimed c
      JOIN public.users    u ON u.id = c.puser
      JOIN public.novels   n ON n.id = c.pnovel
      JOIN public.websites w ON w.id = n.web_id
     ORDER BY c.pdue, c.pid;
END;
$$;

-- 7.5 Acknowledgements. They only touch rows still in 'sending'.
CREATE OR REPLACE FUNCTION public.mark_notifications_sent(notification_ids uuid[])
RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE n int;
BEGIN
    UPDATE public.pending_notifications
       SET status = 'sent', sent_at = now(), error_detail = NULL
     WHERE id = ANY(notification_ids) AND status = 'sending';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION public.mark_notifications_dead(notification_ids uuid[], err_text text)
RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE n int;
BEGIN
    UPDATE public.pending_notifications
       SET status = 'dead', error_detail = left(err_text, 500)
     WHERE id = ANY(notification_ids) AND status = 'sending';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

-- transient failure: back to 'pending' for the next run, or 'dead' when out of attempts/time.
-- The attempt cap is 24: the scraper claims a row once per run, so hourly runs give about 24 attempts,
-- which matches the 24-hour overdue limit below (it was 5 originally; see migrations/002_raise_attempt_cap.sql).
CREATE OR REPLACE FUNCTION public.release_failed_notifications(notification_ids uuid[], err_text text)
RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE n int;
BEGIN
    UPDATE public.pending_notifications
       SET status = CASE WHEN attempts >= 24 OR due_at < now() - interval '24 hours'
                         THEN 'dead'::public.delivery_status
                         ELSE 'pending'::public.delivery_status END,
           error_detail = left(err_text, 500)
     WHERE id = ANY(notification_ids) AND status = 'sending';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

-- 7.6 Site health. Only FULL scrapes call this.
CREATE OR REPLACE FUNCTION public.record_site_result(p_web_id uuid, p_status text, p_error text DEFAULT NULL)
RETURNS TABLE (o_consecutive_failures int, o_failure_alerted boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
#variable_conflict use_column
BEGIN
    IF p_status NOT IN ('ok','degraded','failed') THEN
        RAISE EXCEPTION 'invalid site status: %', p_status;
    END IF;

    INSERT INTO public.site_health AS h
           (web_id, last_status, consecutive_failures, failing_since, last_failed_at, last_ok_at, last_error, updated_at)
    VALUES (p_web_id, p_status,
            CASE WHEN p_status = 'failed' THEN 1 ELSE 0 END,
            CASE WHEN p_status = 'failed' THEN now() END,
            CASE WHEN p_status = 'failed' THEN now() END,
            CASE WHEN p_status <> 'failed' THEN now() END,
            left(p_error, 500), now())
    ON CONFLICT (web_id) DO UPDATE SET
        consecutive_failures = CASE WHEN p_status = 'failed' THEN h.consecutive_failures + 1 ELSE 0 END,
        failing_since        = CASE WHEN p_status = 'failed' THEN COALESCE(h.failing_since, now()) ELSE NULL END,
        last_failed_at       = CASE WHEN p_status = 'failed' THEN now() ELSE h.last_failed_at END,
        last_resolved_at     = CASE WHEN p_status <> 'failed' AND h.last_status = 'failed' THEN now() ELSE h.last_resolved_at END,
        last_ok_at           = CASE WHEN p_status <> 'failed' THEN now() ELSE h.last_ok_at END,
        failure_alerted      = CASE WHEN p_status = 'failed' THEN h.failure_alerted ELSE false END,
        last_error           = COALESCE(left(p_error, 500), h.last_error),
        last_status          = p_status,
        updated_at           = now();

    RETURN QUERY SELECT s.consecutive_failures, s.failure_alerted
                   FROM public.site_health s WHERE s.web_id = p_web_id;
END;
$$;

-- 7.7 Queue statistics for the digest
CREATE OR REPLACE FUNCTION public.queue_stats_24h()
RETURNS TABLE (sent_24h int, retried_24h int, dead_24h int, cancelled_24h int, pending_now int, sending_now int)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
    SELECT
      (count(*) FILTER (WHERE status = 'sent' AND sent_at > now() - interval '24 hours'))::int,
      (count(*) FILTER (WHERE status = 'sent' AND sent_at > now() - interval '24 hours' AND attempts > 1))::int,
      (count(*) FILTER (WHERE status = 'dead' AND COALESCE(last_attempt_at, due_at) > now() - interval '24 hours'))::int,
      (count(*) FILTER (WHERE status = 'cancelled' AND created_at > now() - interval '24 hours'))::int,
      (count(*) FILTER (WHERE status = 'pending'))::int,
      (count(*) FILTER (WHERE status = 'sending'))::int
    FROM public.pending_notifications;
$$;

-- 7.8 Retention (scheduled below)
CREATE OR REPLACE FUNCTION public.cleanup_notification_logs()
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
BEGIN
    DELETE FROM public.pending_notifications WHERE status = 'sent' AND sent_at < now() - interval '30 days';
    DELETE FROM public.pending_notifications WHERE status IN ('dead','cancelled') AND created_at < now() - interval '60 days';
END;
$$;

-- 8. Permissions: functions are for the secret (service_role) key only ----------
DO $$
DECLARE f text;
BEGIN
  FOREACH f IN ARRAY ARRAY[
    'public.update_novels_batch(jsonb)',
    'public.sync_novel_titles(jsonb)',
    'public.maintain_notification_queue(interval)',
    'public.claim_due_notifications(int)',
    'public.mark_notifications_sent(uuid[])',
    'public.mark_notifications_dead(uuid[], text)',
    'public.release_failed_notifications(uuid[], text)',
    'public.record_site_result(uuid, text, text)',
    'public.queue_stats_24h()',
    'public.cleanup_notification_logs()'
  ] LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', f);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
  END LOOP;
END $$;

-- 9. Row Level Security: on, no policies (only the secret key can touch these) ----
ALTER TABLE public.user_site_tiers       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.site_health           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.app_state             ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.user_site_tiers, public.pending_notifications,
                    public.site_health, public.app_state FROM anon, authenticated;

-- 10. Retention job (UTC, like your existing jobs) ------------------------------
SELECT cron.schedule('cleanup-notification-logs', '10 0 * * *', $$SELECT public.cleanup_notification_logs()$$);
