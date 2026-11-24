-- 01_ddl_raw_staging.sql  (Snowflake, v2)
-- Raw landing, staging, apply, reject, run-log tables.
USE SCHEMA SKYPOINTS.CORE;

-- ---------------------------------------------------------------------------
-- RAW: everything VARCHAR so a load can never fail on bad data.
-- Rows are written by the edge converter (src/skypoints/edge.py) in canonical
-- column order, so one COPY works for every source (IND/USA/AUS/GLOBAL).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_MEMBER_FILE (
    load_id           VARCHAR(40)   NOT NULL,   -- one id per batch run (idempotency, lineage)
    source_system     VARCHAR(10)   NOT NULL,   -- IND | USA | AUS | GLOBAL
    source_file       VARCHAR(500)  NOT NULL,
    source_row        NUMBER(18,0)  NOT NULL,   -- 1-based data row within the file
    shape_error       VARCHAR(200),             -- set by the edge when a pipe row has the wrong field count
    feed_date         VARCHAR(10),              -- yyyy-mm-dd, from the file name (NULL if none)
    member_name       VARCHAR(2000),
    member_id         VARCHAR(200),
    enrollment_date   VARCHAR(100),
    last_flight_date  VARCHAR(100),
    tier_code         VARCHAR(100),
    agent_name        VARCHAR(2000),
    state             VARCHAR(100),
    country           VARCHAR(100),
    post_code         VARCHAR(100),
    dob               VARCHAR(100),
    is_active         VARCHAR(100),
    member_type       VARCHAR(100),
    loaded_at         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (load_id);

-- ---------------------------------------------------------------------------
-- STAGING: typed, normalized, derived columns. Rows with an ERROR-severity
-- reject never get here. Transient: reloadable from RAW, no fail-safe cost.
-- Text columns are wider than the spec lengths on purpose; over-length values
-- are reported by the FIELD_LENGTH rule instead of failing the INSERT.
-- ---------------------------------------------------------------------------
CREATE TRANSIENT TABLE IF NOT EXISTS STG_MEMBER (
    load_id           VARCHAR(40)   NOT NULL,
    member_key        VARCHAR(300)  NOT NULL,   -- source_system || '|' || member_id
    source_system     VARCHAR(10)   NOT NULL,
    source_member_id  VARCHAR(200)  NOT NULL,
    member_name       VARCHAR(2000) NOT NULL,
    enrollment_date   DATE,
    last_flight_date  DATE,
    tier_code         VARCHAR(100),
    agent_name        VARCHAR(2000),
    state             VARCHAR(100),
    country_code      VARCHAR(3),
    post_code         NUMBER(5,0),
    dob               DATE,
    is_active         VARCHAR(100),
    member_type       VARCHAR(100),
    as_of_date        DATE          NOT NULL,
    age               NUMBER(4,0),
    stale_member      BOOLEAN,
    feed_date         DATE,
    source_file       VARCHAR(500),
    source_row        NUMBER(18,0),
    loaded_at         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (load_id);

-- STG_APPLY: the ONE winning record per member for a load (latest wins), plus
-- where the member lived before. Lives in a table (not a TEMP table) so the
-- per-country merges can run in parallel sessions.
CREATE TRANSIENT TABLE IF NOT EXISTS STG_APPLY (
    load_id           VARCHAR(40)   NOT NULL,
    member_key        VARCHAR(300)  NOT NULL,
    source_system     VARCHAR(10)   NOT NULL,
    source_member_id  VARCHAR(200)  NOT NULL,
    member_name       VARCHAR(2000) NOT NULL,
    enrollment_date   DATE,
    last_flight_date  DATE,
    tier_code         VARCHAR(100),
    agent_name        VARCHAR(2000),
    state             VARCHAR(100),
    country_code      VARCHAR(3),
    post_code         NUMBER(5,0),
    dob               DATE,
    is_active         VARCHAR(100),
    member_type       VARCHAR(100),
    as_of_date        DATE          NOT NULL,
    age               NUMBER(4,0),
    stale_member      BOOLEAN,
    feed_date         DATE,
    source_file       VARCHAR(500),
    source_row        NUMBER(18,0),
    prev_country      VARCHAR(3)                -- NULL for a brand-new member
)
CLUSTER BY (load_id, country_code);

-- ---------------------------------------------------------------------------
-- REJECTS: one row per failed rule per raw row. source_row = 0 means a
-- file-level issue (e.g. HEADER_SPEC).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS STG_REJECT (
    load_id       VARCHAR(40)  NOT NULL,
    source_system VARCHAR(20),
    source_file   VARCHAR(500),
    source_row    NUMBER(18,0),
    member_key    VARCHAR(300),
    rule_code     VARCHAR(30)  NOT NULL,
    severity      VARCHAR(7)   NOT NULL,        -- ERROR (row excluded) | WARNING (row kept)
    column_name   VARCHAR(50),
    bad_value     VARCHAR(2000),
    reason        VARCHAR(500),
    rejected_at   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- ---------------------------------------------------------------------------
-- Redemption feed: raw NDJSON, one document per line, as VARIANT.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_REDEMPTION_FEED (
    load_id      VARCHAR(40)  NOT NULL,
    source_file  VARCHAR(500) NOT NULL,
    doc          VARIANT      NOT NULL,
    loaded_at    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

-- ---------------------------------------------------------------------------
-- Run log: one row per pipeline step, so a returning operator can see what
-- ran, what it did and where it stopped.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS PIPELINE_RUN_LOG (
    load_id      VARCHAR(40)  NOT NULL,
    step         VARCHAR(60)  NOT NULL,
    status       VARCHAR(10)  NOT NULL,         -- OK | FAILED | SKIPPED
    detail       VARCHAR(2000),
    logged_at    TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
