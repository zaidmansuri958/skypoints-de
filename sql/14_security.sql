-- 14_security.sql  (Snowflake, v2)   -- run ONCE by an admin: `snowflake_runner security`
-- Weakness #10: member data includes personal data (name, date of birth). This adds
--   * least-privilege roles (admin / PII reader / analyst)
--   * dynamic masking of name and date of birth for everyone except the PII roles
--   * row access control: an analyst sees only the countries mapped to their role
--   * PII classification tags, and grants that keep analysts away from RAW / STG / rejects
-- Needs Enterprise edition (masking and row access policies). Creating roles needs
-- SECURITYADMIN or ACCOUNTADMIN; the pipeline itself must run as SKYPOINTS_ADMIN or
-- ACCOUNTADMIN, because the row policy deliberately hides rows from every other role.
USE SCHEMA SKYPOINTS.CORE;

CREATE ROLE IF NOT EXISTS SKYPOINTS_ADMIN;        -- owns the pipeline objects, runs loads
CREATE ROLE IF NOT EXISTS SKYPOINTS_PII_READER;   -- data steward / support: sees unmasked PII
CREATE ROLE IF NOT EXISTS SKYPOINTS_ANALYST;      -- sees masked PII, only their mapped countries

-- Which countries a role may see. Analysts get rows only for countries listed here.
CREATE TABLE IF NOT EXISTS ROLE_COUNTRY_ACCESS (
    role_name    VARCHAR(100) NOT NULL,
    country_code VARCHAR(3)   NOT NULL
);
-- Example: INSERT INTO ROLE_COUNTRY_ACCESS VALUES ('SKYPOINTS_ANALYST', 'IND');

-- Classification tag so PII columns are inventoried and auditable.
CREATE TAG IF NOT EXISTS PII_CLASS ALLOWED_VALUES 'direct', 'quasi';

-- Masking: privileged roles see the value, everyone else sees NULL / a placeholder.
-- (To change a policy later: ALTER MASKING POLICY ... SET BODY -> ...; do not CREATE OR REPLACE
--  an attached policy.)
CREATE MASKING POLICY IF NOT EXISTS MP_PII_DATE AS (v DATE) RETURNS DATE ->
    CASE WHEN IS_ROLE_IN_SESSION('SKYPOINTS_PII_READER') OR IS_ROLE_IN_SESSION('SKYPOINTS_ADMIN')
              OR IS_ROLE_IN_SESSION('ACCOUNTADMIN') THEN v
         ELSE NULL END;

CREATE MASKING POLICY IF NOT EXISTS MP_PII_TEXT AS (v VARCHAR) RETURNS VARCHAR ->
    CASE WHEN IS_ROLE_IN_SESSION('SKYPOINTS_PII_READER') OR IS_ROLE_IN_SESSION('SKYPOINTS_ADMIN')
              OR IS_ROLE_IN_SESSION('ACCOUNTADMIN') THEN v
         ELSE '*** MASKED ***' END;

-- Row access: admin sees everything; any other role sees only countries mapped to it.
-- (parameter is named cc so it cannot be confused with the column in the subquery)
CREATE ROW ACCESS POLICY IF NOT EXISTS RAP_MEMBER_COUNTRY AS (cc VARCHAR) RETURNS BOOLEAN ->
    IS_ROLE_IN_SESSION('SKYPOINTS_ADMIN') OR IS_ROLE_IN_SESSION('ACCOUNTADMIN')
    OR EXISTS (SELECT 1 FROM ROLE_COUNTRY_ACCESS r
                WHERE r.country_code = cc AND IS_ROLE_IN_SESSION(r.role_name));

-- History and the routing index carry country_code too, so they get the same row policy.
-- (best effort: a re-run finds it attached already; the runner tolerates that)
ALTER TABLE MEMBER_HISTORY ADD ROW ACCESS POLICY RAP_MEMBER_COUNTRY ON (country_code);
ALTER TABLE MEMBER_INDEX   ADD ROW ACCESS POLICY RAP_MEMBER_COUNTRY ON (country_code);

-- Least privilege: analysts get the curated read surface only, never RAW / STG / rejects
-- (STG_REJECT.bad_value holds raw, unmasked values).
GRANT USAGE ON DATABASE SKYPOINTS                 TO ROLE SKYPOINTS_ANALYST;
GRANT USAGE ON SCHEMA SKYPOINTS.CORE              TO ROLE SKYPOINTS_ANALYST;
GRANT USAGE ON WAREHOUSE SKYPOINTS_WH             TO ROLE SKYPOINTS_ANALYST;
GRANT SELECT ON VIEW MEMBER_ALL                   TO ROLE SKYPOINTS_ANALYST;
GRANT SELECT ON VIEW REDEMPTION_WITH_MEMBER       TO ROLE SKYPOINTS_ANALYST;
GRANT SELECT ON VIEW V_MEMBER_MOVES               TO ROLE SKYPOINTS_ANALYST;
GRANT SELECT ON TABLE MEMBER_HISTORY              TO ROLE SKYPOINTS_ANALYST;
GRANT SELECT ON TABLE REDEMPTION_FACT             TO ROLE SKYPOINTS_ANALYST;

GRANT USAGE ON DATABASE SKYPOINTS                 TO ROLE SKYPOINTS_PII_READER;
GRANT USAGE ON SCHEMA SKYPOINTS.CORE              TO ROLE SKYPOINTS_PII_READER;
GRANT USAGE ON WAREHOUSE SKYPOINTS_WH             TO ROLE SKYPOINTS_PII_READER;
GRANT SELECT ON VIEW MEMBER_ALL                   TO ROLE SKYPOINTS_PII_READER;
GRANT SELECT ON VIEW REDEMPTION_WITH_MEMBER       TO ROLE SKYPOINTS_PII_READER;
GRANT SELECT ON VIEW V_MEMBER_MOVES               TO ROLE SKYPOINTS_PII_READER;
GRANT SELECT ON TABLE MEMBER_HISTORY              TO ROLE SKYPOINTS_PII_READER;
GRANT SELECT ON TABLE REDEMPTION_FACT             TO ROLE SKYPOINTS_PII_READER;
-- The identity-match view compares NAMES. For an analyst the names are masked, so every
-- candidate would "match" on the placeholder text: it is therefore PII-reader only.
GRANT SELECT ON VIEW V_MEMBER_MATCH_CANDIDATES    TO ROLE SKYPOINTS_PII_READER;
