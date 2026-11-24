-- 15b_parity_compare.sql  (Snowflake, v2)
-- Each statement returns ONE ROW PER DIFFERENCE between the Python oracle (golden files)
-- and the Snowflake staging output for the sample load. Zero rows from both = the SQL and
-- Python implement the same rules on this data.
-- Template variables: ${LOAD_ID}
USE SCHEMA SKYPOINTS.CORE;

-- Staged rows: parsed values, derived age / stale flag, country normalisation.
SELECT 'staging_only_in_expected' AS diff, d.* FROM (
    SELECT * FROM PARITY_STG_EXPECTED
    MINUS
    SELECT source_file, TO_VARCHAR(source_row), member_key, member_name,
           COALESCE(TO_VARCHAR(enrollment_date), ''), COALESCE(TO_VARCHAR(last_flight_date), ''),
           COALESCE(TO_VARCHAR(dob), ''), COALESCE(tier_code, ''), COALESCE(state, ''),
           COALESCE(country_code, ''), COALESCE(TO_VARCHAR(post_code), ''), COALESCE(is_active, ''),
           COALESCE(member_type, ''), COALESCE(TO_VARCHAR(age), ''), COALESCE(TO_VARCHAR(stale_member), '')
      FROM STG_MEMBER WHERE load_id = '${LOAD_ID}'
) d
UNION ALL
SELECT 'staging_only_in_actual', d.* FROM (
    SELECT source_file, TO_VARCHAR(source_row), member_key, member_name,
           COALESCE(TO_VARCHAR(enrollment_date), ''), COALESCE(TO_VARCHAR(last_flight_date), ''),
           COALESCE(TO_VARCHAR(dob), ''), COALESCE(tier_code, ''), COALESCE(state, ''),
           COALESCE(country_code, ''), COALESCE(TO_VARCHAR(post_code), ''), COALESCE(is_active, ''),
           COALESCE(member_type, ''), COALESCE(TO_VARCHAR(age), ''), COALESCE(TO_VARCHAR(stale_member), '')
      FROM STG_MEMBER WHERE load_id = '${LOAD_ID}'
    MINUS
    SELECT * FROM PARITY_STG_EXPECTED
) d;

-- Rejects: which rules fired on which rows, at which severity.
SELECT 'rejects_only_in_expected' AS diff, d.* FROM (
    SELECT * FROM PARITY_REJECT_EXPECTED
    MINUS
    SELECT DISTINCT source_file, TO_VARCHAR(source_row), rule_code, severity
      FROM STG_REJECT WHERE load_id = '${LOAD_ID}' AND source_system <> 'REDEMPTION'
) d
UNION ALL
SELECT 'rejects_only_in_actual', d.* FROM (
    SELECT DISTINCT source_file, TO_VARCHAR(source_row), rule_code, severity
      FROM STG_REJECT WHERE load_id = '${LOAD_ID}' AND source_system <> 'REDEMPTION'
    MINUS
    SELECT * FROM PARITY_REJECT_EXPECTED
) d;
