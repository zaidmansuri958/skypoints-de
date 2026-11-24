-- 08_history_index.sql  (Snowflake, v2)
-- After the country tables are merged: keep MEMBER_INDEX and MEMBER_HISTORY in step.
-- Template variables: ${LOAD_ID}.  Both statements are idempotent (safe to re-run).
USE SCHEMA SKYPOINTS.CORE;

-- 1. Routing index: member -> current country.
MERGE INTO MEMBER_INDEX i
USING (SELECT member_key, source_system, source_member_id, country_code, feed_date
         FROM STG_APPLY WHERE load_id = '${LOAD_ID}') a
   ON i.member_key = a.member_key
WHEN MATCHED THEN UPDATE SET
    country_code = a.country_code, feed_date = a.feed_date, updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (member_key, source_system, member_id, country_code, feed_date)
VALUES (a.member_key, a.source_system, a.source_member_id, a.country_code, a.feed_date);

-- 2a. SCD2: close the current history row when any tracked attribute changed.
--     Effective date = feed date, else the run's as-of date (same as history.effective_date).
UPDATE MEMBER_HISTORY h
   SET valid_to = a.eff, is_current = FALSE
  FROM (SELECT member_key, country_code, tier_code, is_active, state,
               COALESCE(feed_date, as_of_date) AS eff
          FROM STG_APPLY WHERE load_id = '${LOAD_ID}') a
 WHERE h.member_key = a.member_key
   AND h.is_current
   AND NOT (    h.country_code IS NOT DISTINCT FROM a.country_code
            AND h.tier_code   IS NOT DISTINCT FROM a.tier_code
            AND h.is_active   IS NOT DISTINCT FROM a.is_active
            AND h.state       IS NOT DISTINCT FROM a.state);

-- 2b. SCD2: open a new current row for members that have none (new members and
--     members whose row was just closed).
INSERT INTO MEMBER_HISTORY (member_key, valid_from, valid_to, is_current,
                            country_code, tier_code, is_active, state, load_id)
SELECT a.member_key, COALESCE(a.feed_date, a.as_of_date), NULL, TRUE,
       a.country_code, a.tier_code, a.is_active, a.state, a.load_id
  FROM STG_APPLY a
 WHERE a.load_id = '${LOAD_ID}'
   AND NOT EXISTS (SELECT 1 FROM MEMBER_HISTORY h
                    WHERE h.member_key = a.member_key AND h.is_current);

-- Example questions this answers:
--   Where / what tier was a member on a date?
--     SELECT * FROM MEMBER_HISTORY
--      WHERE member_key = 'GLOBAL|223457'
--        AND valid_from <= DATE '2024-03-01' AND (valid_to IS NULL OR DATE '2024-03-01' < valid_to);
--   Who moved country, and when?   SELECT * FROM V_MEMBER_MOVES ORDER BY moved_on;
