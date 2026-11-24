-- 00_setup.sql  (Snowflake, v2)
-- One-time environment: database, schema, a small auto-suspending warehouse, file formats
-- and the internal landing stage. Safe to re-run. Sized for a free trial account:
-- an X-SMALL warehouse that suspends after 60 seconds idle costs almost nothing.
CREATE DATABASE IF NOT EXISTS SKYPOINTS;
CREATE SCHEMA IF NOT EXISTS SKYPOINTS.CORE;
USE SCHEMA SKYPOINTS.CORE;

CREATE WAREHOUSE IF NOT EXISTS SKYPOINTS_WH
    WITH WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;
USE WAREHOUSE SKYPOINTS_WH;

-- Canonical member CSV written by the edge converter (src/skypoints/edge.py).
CREATE FILE FORMAT IF NOT EXISTS FF_CANON_CSV
    TYPE = CSV FIELD_DELIMITER = ',' SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    EMPTY_FIELD_AS_NULL = TRUE ENCODING = 'UTF8';

-- Partner redemption feed: newline-delimited JSON, one document per line.
CREATE FILE FORMAT IF NOT EXISTS FF_JSON
    TYPE = JSON STRIP_OUTER_ARRAY = FALSE;

CREATE STAGE IF NOT EXISTS LANDING;
