-- 06_prepare_apply.sql  (Snowflake, v2)
-- "Latest record wins": pick ONE winning record per member for this load, and remember where
-- the member lived before (prev_country) so a country move can be applied.
--   order: newest feed_date, then newest last_flight_date, then last row, then file name
--          (same ordering as transform._rank; NULLs lose)
--   an OLDER file never overwrites a NEWER stored record (late / replayed files are skipped)
-- Result lives in STG_APPLY (a real table) so the per-country merges can run in parallel sessions.
-- Template variables: ${LOAD_ID}
USE SCHEMA SKYPOINTS.CORE;

DELETE FROM STG_APPLY WHERE load_id = '${LOAD_ID}';

INSERT INTO STG_APPLY (
    load_id, member_key, source_system, source_member_id, member_name,
    enrollment_date, last_flight_date, tier_code, agent_name, state,
    country_code, post_code, dob, is_active, member_type,
    as_of_date, age, stale_member, feed_date, source_file, source_row, prev_country)
SELECT
    w.load_id, w.member_key, w.source_system, w.source_member_id, w.member_name,
    w.enrollment_date, w.last_flight_date, w.tier_code, w.agent_name, w.state,
    w.country_code, w.post_code, w.dob, w.is_active, w.member_type,
    w.as_of_date, w.age, w.stale_member, w.feed_date, w.source_file, w.source_row,
    i.country_code AS prev_country
  FROM (
        SELECT * FROM STG_MEMBER
         WHERE load_id = '${LOAD_ID}'
        QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY member_key
                    ORDER BY COALESCE(feed_date, DATE '0001-01-01') DESC,
                             COALESCE(last_flight_date, DATE '0001-01-01') DESC,
                             source_row DESC, source_file DESC) = 1
       ) w
  LEFT JOIN MEMBER_INDEX i ON i.member_key = w.member_key
 WHERE i.member_key IS NULL
    OR w.feed_date IS NULL
    OR i.feed_date IS NULL
    OR w.feed_date >= i.feed_date;
