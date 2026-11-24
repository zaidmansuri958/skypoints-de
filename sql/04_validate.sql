-- 04_validate.sql  (Snowflake, v2)
-- Data-quality rules for one load. Writes STG_REJECT. Reads TMP_PARSED (see 03b_parse_load.sql).
--   ERROR   -> the row is excluded from staging (05 filters on it)
--   WARNING -> the row is kept and the issue is logged
-- Template variables: ${LOAD_ID} ${AS_OF}
-- Every rule mirrors src/skypoints/stage.py / validate.py; tests/golden + 15_parity prove it.
-- Run order per load: 03b -> 04 -> (runner adds HEADER_SPEC rows) -> gate -> 05 -> 06 -> 07 -> ...
USE SCHEMA SKYPOINTS.CORE;

-- HEADER_SPEC rows (source_row = 0) are written by the runner AFTER this step, so this
-- only clears member rule output; redemption rejects (10_redemptions.sql) are left alone.
DELETE FROM STG_REJECT WHERE load_id = '${LOAD_ID}' AND source_system <> 'REDEMPTION';

-- ---------------------------------------------------------------------------
-- Row-level rules
-- ---------------------------------------------------------------------------
INSERT INTO STG_REJECT (load_id, source_system, source_file, source_row, member_key,
                        rule_code, severity, column_name, bad_value, reason)

-- ROW_SHAPE: pipe row with the wrong number of fields
SELECT load_id, source_system, source_file, source_row, member_key,
       'ROW_SHAPE', 'ERROR', NULL::VARCHAR, NULL::VARCHAR, shape_error
  FROM TMP_PARSED WHERE shape_error IS NOT NULL

-- MANDATORY: spec says name, member id and enrollment date are required
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'MANDATORY', 'ERROR', 'member_name', NULL, 'member name is required'
  FROM TMP_PARSED WHERE member_name_c IS NULL
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'MANDATORY', 'ERROR', 'member_id', NULL, 'member id is required'
  FROM TMP_PARSED WHERE member_id_c IS NULL
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'MANDATORY', 'ERROR', 'enrollment_date', NULL, 'enrollment date is required'
  FROM TMP_PARSED WHERE enroll_raw IS NULL

-- ID_FORMAT: member ids are numeric
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'ID_FORMAT', 'WARNING', 'member_id', member_id_c, 'member id not numeric'
  FROM TMP_PARSED WHERE member_id_c IS NOT NULL AND NOT REGEXP_LIKE(member_id_c, '[0-9]+')

-- BAD_DATE: value present but unparseable (e.g. AUS 2021-13-13). Enrollment is mandatory -> ERROR.
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'BAD_DATE', 'ERROR', 'enrollment_date', enroll_raw, 'enrollment_date is not a valid date'
  FROM TMP_PARSED WHERE enroll_raw IS NOT NULL AND enroll_d IS NULL
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'BAD_DATE', 'WARNING', 'last_flight_date', flight_raw, 'last_flight_date is not a valid date'
  FROM TMP_PARSED WHERE flight_raw IS NOT NULL AND flight_d IS NULL
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'BAD_DATE', 'WARNING', 'dob', dob_raw, 'dob is not a valid date'
  FROM TMP_PARSED WHERE dob_raw IS NOT NULL AND dob_d IS NULL

-- DATE_ORDER / AGE_RANGE
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'DATE_ORDER', 'ERROR', 'enrollment_date', TO_VARCHAR(enroll_d), 'enrollment date is in the future'
  FROM TMP_PARSED WHERE enroll_d > DATE '${AS_OF}'
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'DATE_ORDER', 'WARNING', 'last_flight_date', TO_VARCHAR(flight_d), 'flight date is before enrollment date'
  FROM TMP_PARSED WHERE flight_d < enroll_d
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'DATE_ORDER', 'WARNING', 'dob', TO_VARCHAR(dob_d), 'date of birth is in the future'
  FROM TMP_PARSED WHERE dob_d > DATE '${AS_OF}'
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'AGE_RANGE', 'WARNING', 'dob', TO_VARCHAR(dob_d), 'age outside 0-110'
  FROM TMP_PARSED
 WHERE dob_d IS NOT NULL
   AND (CALC_AGE(dob_d, DATE '${AS_OF}') < 0 OR CALC_AGE(dob_d, DATE '${AS_OF}') > 110)

-- Country: only the GLOBAL feed carries a Country column
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'UNKNOWN_COUNTRY', 'ERROR', 'country', country_raw, 'country not in registry'
  FROM TMP_PARSED WHERE fixed_country IS NULL AND country_code IS NULL
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'COUNTRY_ALIAS', 'WARNING', 'country', country_raw, 'non-ISO code normalized to ' || country_code
  FROM TMP_PARSED
 WHERE fixed_country IS NULL AND country_code IS NOT NULL AND UPPER(country_raw) <> country_code

-- Domains
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'TIER_CODE', 'WARNING', 'tier_code', tier_c, 'tier code not in the allowed list'
  FROM TMP_PARSED
 WHERE tier_c IS NOT NULL AND tier_c NOT IN (SELECT value FROM DIM_DOMAIN WHERE domain = 'TIER')
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'ACTIVE_FLAG', 'WARNING', 'is_active', active_c, 'active flag not in the allowed list'
  FROM TMP_PARSED
 WHERE active_c IS NOT NULL AND active_c NOT IN (SELECT value FROM DIM_DOMAIN WHERE domain = 'ACTIVE')
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'POST_CODE', 'WARNING', 'post_code', post_raw, 'post code must be 1-5 digits'
  FROM TMP_PARSED WHERE post_raw IS NOT NULL AND post_n IS NULL

-- FIELD_LENGTH: values longer than the spec length (staging columns are wider, so INSERTs never fail)
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'FIELD_LENGTH', 'WARNING', 'member_name', member_name_c, 'member_name is longer than the spec length 255'
  FROM TMP_PARSED WHERE LENGTH(member_name_c) > 255
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'FIELD_LENGTH', 'WARNING', 'member_id', member_id_c, 'member_id is longer than the spec length 18'
  FROM TMP_PARSED WHERE LENGTH(member_id_c) > 18
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'FIELD_LENGTH', 'WARNING', 'tier_code', tier_c, 'tier_code is longer than the spec length 5'
  FROM TMP_PARSED WHERE LENGTH(tier_c) > 5
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'FIELD_LENGTH', 'WARNING', 'state', state_c, 'state is longer than the spec length 5'
  FROM TMP_PARSED WHERE LENGTH(state_c) > 5
UNION ALL
SELECT load_id, source_system, source_file, source_row, member_key,
       'FIELD_LENGTH', 'WARNING', 'agent_name', agent_c, 'agent_name is longer than the spec length 255'
  FROM TMP_PARSED WHERE LENGTH(agent_c) > 255;

-- ---------------------------------------------------------------------------
-- Batch-level rules. They look only at rows with no ERROR, i.e. rows that will be staged.
-- ---------------------------------------------------------------------------

-- DUP_KEY: key-column uniqueness. The latest-wins step keeps one row; this makes the repeat visible.
INSERT INTO STG_REJECT (load_id, source_system, source_file, source_row, member_key,
                        rule_code, severity, column_name, bad_value, reason)
SELECT p.load_id, p.source_system, p.source_file, p.source_row, p.member_key,
       'DUP_KEY', 'WARNING', 'member_key', p.member_key, 'duplicate member in batch; latest record wins'
  FROM TMP_PARSED p
 WHERE NOT EXISTS (SELECT 1 FROM STG_REJECT e
                    WHERE e.load_id = p.load_id AND e.source_file = p.source_file
                      AND e.source_row = p.source_row AND e.severity = 'ERROR')
QUALIFY COUNT(*) OVER (PARTITION BY p.member_key) > 1;

-- ID_FORMAT (length): in the GLOBAL feed, flag ids whose length differs from the file's most
-- common length (a truncated or zero-stripped id). Ties go to the longer length, like validate.py.
CREATE OR REPLACE TEMPORARY TABLE TMP_ID_NORM AS
WITH ok AS (
    SELECT p.source_file, LENGTH(p.member_id_c) AS len
      FROM TMP_PARSED p
     WHERE p.source_system = 'GLOBAL'
       AND NOT EXISTS (SELECT 1 FROM STG_REJECT e
                        WHERE e.load_id = p.load_id AND e.source_file = p.source_file
                          AND e.source_row = p.source_row AND e.severity = 'ERROR')
), lens AS (
    SELECT source_file, len, COUNT(*) AS c FROM ok GROUP BY source_file, len
)
SELECT source_file, len AS norm_len
  FROM lens
QUALIFY ROW_NUMBER() OVER (PARTITION BY source_file ORDER BY c DESC, len DESC) = 1;

INSERT INTO STG_REJECT (load_id, source_system, source_file, source_row, member_key,
                        rule_code, severity, column_name, bad_value, reason)
SELECT p.load_id, p.source_system, p.source_file, p.source_row, p.member_key,
       'ID_FORMAT', 'WARNING', 'member_id', p.member_id_c,
       'member id length ' || LENGTH(p.member_id_c) || ' differs from file norm ' || n.norm_len
         || ' (possible truncation)'
  FROM TMP_PARSED p
  JOIN TMP_ID_NORM n ON n.source_file = p.source_file
 WHERE p.source_system = 'GLOBAL'
   AND LENGTH(p.member_id_c) <> n.norm_len
   AND NOT EXISTS (SELECT 1 FROM STG_REJECT e
                    WHERE e.load_id = p.load_id AND e.source_file = p.source_file
                      AND e.source_row = p.source_row AND e.severity = 'ERROR');
