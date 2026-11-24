-- 03b_parse_load.sql  (Snowflake, v2)
-- Parse this load's raw rows ONCE into a session-scoped temp table; 04_validate.sql and
-- 05_stage_member.sql both read it. Run 03b, 04 and 05 in the SAME session (the runner does).
-- Template variables: ${LOAD_ID}
USE SCHEMA SKYPOINTS.CORE;

CREATE OR REPLACE TEMPORARY TABLE TMP_PARSED AS
SELECT * FROM V_RAW_MEMBER_PARSED WHERE load_id = '${LOAD_ID}';
