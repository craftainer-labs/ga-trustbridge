-- Deletes Umami analytics data older than 25 months (the CNIL's recommended
-- maximum retention for audience measurement). Umami OSS has no retention
-- setting. Run manually; see README.md.
BEGIN;
DELETE FROM event_data   WHERE created_at < now() - interval '25 months';
DELETE FROM website_event WHERE created_at < now() - interval '25 months';
DELETE FROM session_data WHERE created_at < now() - interval '25 months';
DELETE FROM session      WHERE created_at < now() - interval '25 months';
COMMIT;
