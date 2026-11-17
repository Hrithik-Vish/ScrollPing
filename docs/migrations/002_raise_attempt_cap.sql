-- 002: raise the delivery attempt cap from 5 to 24.
--
-- Why: the scraper claims a queued row once per run (an attempt is counted when a row is claimed). With hourly
-- runs a row therefore gets about 24 attempts in the 24 hours it is allowed to live. A cap of 5 would give up
-- after roughly five hours of Telegram trouble, long before the 24-hour limit.
--
-- Needed only on a database that already ran the ORIGINAL 001 (cap 5). A fresh database that runs the current
-- 001 already has the cap of 24. Safe to run more than once, and nothing is interrupted: CREATE OR REPLACE
-- keeps the function's existing privileges, and the grants below are repeated only to make that explicit.

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

REVOKE ALL ON FUNCTION public.release_failed_notifications(uuid[], text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.release_failed_notifications(uuid[], text) TO service_role;

-- Check (should return 24): the number in the function body.
SELECT (regexp_match(pg_get_functiondef('public.release_failed_notifications(uuid[], text)'::regprocedure),
                     'attempts >= (\d+)'))[1]::int AS attempt_cap;
