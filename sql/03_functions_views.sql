-- 03_functions_views.sql  (Snowflake, v2)
-- Parsing and derivation, defined ONCE. Everything downstream (validation, staging, the
-- derived-column refresh, parity checks) calls these, so the rules cannot differ between
-- steps. Per-source formats, countries and allowed values come from DIM_* tables that the
-- runner seeds from src/skypoints/config.py, so they are not repeated here either.
USE SCHEMA SKYPOINTS.CORE;

-- Trim; map empty / NULL-like tokens to SQL NULL (same tokens as normalize.clean).
CREATE OR REPLACE FUNCTION CLEAN_TXT(s VARCHAR)
RETURNS VARCHAR
AS
$$
    IFF(UPPER(TRIM(s)) IN ('', 'NULL', 'NONE', 'N/A', 'NA'), NULL, TRIM(s))
$$;

-- Parse a date using a named source format (same names as normalize.parse_date).
-- Present-but-invalid values (e.g. month 13) return NULL; callers tell them apart from
-- genuinely empty values by also checking CLEAN_TXT(val).
CREATE OR REPLACE FUNCTION PARSE_SRC_DATE(val VARCHAR, fmt VARCHAR)
RETURNS DATE
AS
$$
    CASE fmt
        WHEN 'MDY_SLASH'   THEN TRY_TO_DATE(CLEAN_TXT(val), 'MM/DD/YYYY')
        WHEN 'ISO'         THEN TRY_TO_DATE(CLEAN_TXT(val), 'YYYY-MM-DD')
        WHEN 'YMD_COMPACT' THEN IFF(REGEXP_LIKE(CLEAN_TXT(val), '[0-9]{8}'),
                                    TRY_TO_DATE(CLEAN_TXT(val), 'YYYYMMDD'), NULL)
        -- mddyyyy / dmmyyyy exports drop the leading zero (6152022 -> 06152022): left-pad to 8
        WHEN 'MDY_COMPACT' THEN IFF(REGEXP_LIKE(CLEAN_TXT(val), '[0-9]{7,8}'),
                                    TRY_TO_DATE(LPAD(CLEAN_TXT(val), 8, '0'), 'MMDDYYYY'), NULL)
        WHEN 'DMY_COMPACT' THEN IFF(REGEXP_LIKE(CLEAN_TXT(val), '[0-9]{7,8}'),
                                    TRY_TO_DATE(LPAD(CLEAN_TXT(val), 8, '0'), 'DDMMYYYY'), NULL)
    END
$$;

-- Completed years as of as_of (birthday not yet reached -> one less). Same as normalize.compute_age.
CREATE OR REPLACE FUNCTION CALC_AGE(dob DATE, as_of DATE)
RETURNS NUMBER
AS
$$
    IFF(dob IS NULL, NULL,
        DATEDIFF('year', dob, as_of) - IFF(TO_CHAR(as_of, 'MMDD') < TO_CHAR(dob, 'MMDD'), 1, 0))
$$;

-- More than `days` days since the last flight; never flown counts as stale. Same as normalize.is_stale.
CREATE OR REPLACE FUNCTION IS_STALE(last_flight DATE, as_of DATE, days NUMBER)
RETURNS BOOLEAN
AS
$$
    COALESCE(DATEDIFF('day', last_flight, as_of) > days, TRUE)
$$;

-- ---------------------------------------------------------------------------
-- The one place raw rows are parsed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW V_RAW_MEMBER_PARSED AS
SELECT
    r.load_id, r.source_system, r.source_file, r.source_row, r.shape_error,
    TRY_TO_DATE(r.feed_date, 'YYYY-MM-DD')                AS feed_d,
    CLEAN_TXT(r.member_id)                                AS member_id_c,
    r.source_system || '|' || CLEAN_TXT(r.member_id)      AS member_key,
    CLEAN_TXT(r.member_name)                              AS member_name_c,
    CLEAN_TXT(r.enrollment_date)                          AS enroll_raw,
    CLEAN_TXT(r.last_flight_date)                         AS flight_raw,
    CLEAN_TXT(r.dob)                                      AS dob_raw,
    PARSE_SRC_DATE(r.enrollment_date, s.enroll_fmt)       AS enroll_d,
    PARSE_SRC_DATE(r.last_flight_date, s.flight_fmt)      AS flight_d,
    PARSE_SRC_DATE(r.dob, s.dob_fmt)                      AS dob_d,
    UPPER(CLEAN_TXT(r.tier_code))                         AS tier_c,
    CLEAN_TXT(r.agent_name)                               AS agent_c,
    UPPER(CLEAN_TXT(r.state))                             AS state_c,
    s.fixed_country                                       AS fixed_country,
    CLEAN_TXT(r.country)                                  AS country_raw,
    COALESCE(s.fixed_country, a.country_code)             AS country_code,
    CLEAN_TXT(r.post_code)                                AS post_raw,
    IFF(REGEXP_LIKE(CLEAN_TXT(r.post_code), '[0-9]{1,5}'), TRY_TO_NUMBER(CLEAN_TXT(r.post_code)), NULL) AS post_n,
    UPPER(CLEAN_TXT(r.is_active))                         AS active_c,
    UPPER(CLEAN_TXT(r.member_type))                       AS mtype_c
  FROM RAW_MEMBER_FILE r
  JOIN DIM_SOURCE s              ON s.source_system = r.source_system
  LEFT JOIN DIM_COUNTRY_ALIAS a  ON a.alias = UPPER(CLEAN_TXT(r.country));
