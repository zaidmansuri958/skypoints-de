-- 15a_parity_tables.sql  (Snowflake, v2)
-- Holding tables for the golden files (tests/golden/*.csv), which are generated from the
-- Python implementation. The runner (`demo` / `parity`) loads them, then 15b diffs them
-- against what the SQL pipeline staged. All columns are text so the comparison is exact.
USE SCHEMA SKYPOINTS.CORE;

CREATE OR REPLACE TRANSIENT TABLE PARITY_STG_EXPECTED (
    source_file VARCHAR, source_row VARCHAR, member_key VARCHAR, member_name VARCHAR,
    enrollment_date VARCHAR, last_flight_date VARCHAR, dob VARCHAR, tier_code VARCHAR,
    state VARCHAR, country_code VARCHAR, post_code VARCHAR, is_active VARCHAR,
    member_type VARCHAR, age VARCHAR, stale_member VARCHAR);

CREATE OR REPLACE TRANSIENT TABLE PARITY_REJECT_EXPECTED (
    source_file VARCHAR, source_row VARCHAR, rule_code VARCHAR, severity VARCHAR);
