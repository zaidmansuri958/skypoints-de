-- 05_stage_member.sql  (Snowflake, v2)
-- TMP_PARSED -> STG_MEMBER with the derived columns:
--   age          completed years from DOB as of ${AS_OF}
--   stale_member TRUE when more than ${STALE_DAYS} days since last flight (never flown = TRUE)
-- Rows with an ERROR-severity reject (04_validate.sql) are excluded; they stay in STG_REJECT.
-- Template variables: ${LOAD_ID} ${AS_OF} ${STALE_DAYS}.  Runs in the same session as 03b/04.
USE SCHEMA SKYPOINTS.CORE;

DELETE FROM STG_MEMBER WHERE load_id = '${LOAD_ID}';

INSERT INTO STG_MEMBER (
    load_id, member_key, source_system, source_member_id, member_name,
    enrollment_date, last_flight_date, tier_code, agent_name, state,
    country_code, post_code, dob, is_active, member_type,
    as_of_date, age, stale_member, feed_date, source_file, source_row)
SELECT
    p.load_id, p.member_key, p.source_system, p.member_id_c, p.member_name_c,
    p.enroll_d, p.flight_d, p.tier_c, p.agent_c, p.state_c,
    p.country_code, p.post_n, p.dob_d, p.active_c, p.mtype_c,
    DATE '${AS_OF}',
    CALC_AGE(p.dob_d, DATE '${AS_OF}'),
    IS_STALE(p.flight_d, DATE '${AS_OF}', ${STALE_DAYS}),
    p.feed_d, p.source_file, p.source_row
  FROM TMP_PARSED p
 WHERE NOT EXISTS (SELECT 1 FROM STG_REJECT e
                    WHERE e.load_id = p.load_id AND e.source_file = p.source_file
                      AND e.source_row = p.source_row AND e.severity = 'ERROR');
