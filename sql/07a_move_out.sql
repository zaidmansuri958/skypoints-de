-- 07a_move_out.sql  (Snowflake, v2)
-- Phase 1 of the country split, run once per country table: remove members who now belong
-- to a DIFFERENT country. All phase-1 statements finish before any phase-2 upsert starts,
-- so a moved member is never present in two country tables at the same time.
-- Template variables: ${LOAD_ID} ${COUNTRY_CODE} ${TARGET_TABLE}
USE SCHEMA SKYPOINTS.CORE;

DELETE FROM ${TARGET_TABLE} t
 USING STG_APPLY a
 WHERE a.load_id = '${LOAD_ID}'
   AND t.member_key = a.member_key
   AND a.prev_country = '${COUNTRY_CODE}'
   AND a.country_code <> '${COUNTRY_CODE}';
