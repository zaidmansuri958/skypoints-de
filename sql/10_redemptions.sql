-- 10_redemptions.sql  (Snowflake, v2)
-- Flatten the partner JSON feed, validate it, merge it into REDEMPTION_FACT, link it to
-- members, and expose a join that never carries a stale country.
-- Template variables: ${LOAD_ID}.  Requires CLEAN_TXT() from 03_functions_views.sql.
--
-- Fixes weakness #7: the fact stores the stable member_key (resolved at load time and
-- re-resolved for orphans on every later load). The member's CURRENT country is looked up
-- at read time through the clustered member_key, so a country move never leaves a wrong
-- country on a redemption, and the join does not depend on a view over every country table
-- being pruned by country_code.
USE SCHEMA SKYPOINTS.CORE;

-- ---------------------------------------------------------------------------
-- 1. One row per transaction, with validity flags computed once.
--    Optional doc.source_system names the id namespace of member_id (default GLOBAL),
--    so a feed that adds it can reference the per-country id spaces with no code change.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW V_RAW_REDEMPTION_FLAT AS
WITH d AS (
    SELECT r.load_id, r.source_file, r.doc,
           UPPER(COALESCE(CLEAN_TXT(r.doc:source_system::VARCHAR), 'GLOBAL')) AS source_system
      FROM RAW_REDEMPTION_FEED r
     WHERE IS_ARRAY(r.doc:redemptions)
), f AS (
    SELECT d.load_id, d.source_file, d.source_system, fl.index + 1 AS txn_no,
           CLEAN_TXT(d.doc:member_id::VARCHAR)  AS member_id,
           CASE WHEN REGEXP_LIKE(d.doc:feed_date::VARCHAR, '[0-9]{8}')
                THEN TRY_TO_DATE(d.doc:feed_date::VARCHAR, 'YYYYMMDD') END AS feed_d,
           CLEAN_TXT(fl.value:txn_id::VARCHAR)  AS txn_id,
           CASE WHEN REGEXP_LIKE(fl.value:txn_date::VARCHAR, '[0-9]{8}')
                THEN TRY_TO_DATE(fl.value:txn_date::VARCHAR, 'YYYYMMDD') END AS txn_d,
           fl.value:txn_date::VARCHAR           AS txn_date_raw,
           fl.value:partner::VARCHAR            AS partner,
           fl.value:miles_redeemed              AS miles_v,
           UPPER(CLEAN_TXT(fl.value:status::VARCHAR)) AS status_c
      FROM d, LATERAL FLATTEN(input => d.doc:redemptions) fl
)
SELECT f.*,
       (s.source_system IS NULL)                                   AS unknown_source,
       (f.txn_id IS NULL)                                          AS bad_txn_id,
       (f.member_id IS NULL)                                       AS bad_member,
       (f.txn_d IS NULL)                                           AS bad_date,
       NOT (IS_INTEGER(f.miles_v) AND COALESCE(TRY_TO_NUMBER(f.miles_v::VARCHAR), 0) > 0) AS bad_miles,
       COALESCE(f.txn_d > f.feed_d, FALSE)                         AS txn_after_feed,
       (f.status_c IS NULL OR f.status_c NOT IN (SELECT value FROM DIM_DOMAIN WHERE domain = 'REDEMPTION_STATUS'))
                                                                   AS bad_status,
       NOT (   s.source_system IS NULL OR f.txn_id IS NULL OR f.member_id IS NULL OR f.txn_d IS NULL
            OR NOT (IS_INTEGER(f.miles_v) AND COALESCE(TRY_TO_NUMBER(f.miles_v::VARCHAR), 0) > 0)) AS is_valid
  FROM f
  LEFT JOIN DIM_SOURCE s ON s.source_system = f.source_system;

-- ---------------------------------------------------------------------------
-- 2. Rejects (ERROR rows are excluded from the merge; WARNING rows are kept).
-- ---------------------------------------------------------------------------
DELETE FROM STG_REJECT WHERE load_id = '${LOAD_ID}' AND source_system = 'REDEMPTION';

INSERT INTO STG_REJECT (load_id, source_system, source_file, source_row, member_key, rule_code, severity,
                        column_name, bad_value, reason)
SELECT load_id, 'REDEMPTION', source_file, txn_no, txn_id, 'UNKNOWN_SOURCE', 'ERROR', 'source_system',
       source_system, 'source_system is not a known member source'
  FROM V_RAW_REDEMPTION_FLAT WHERE load_id = '${LOAD_ID}' AND unknown_source
UNION ALL
SELECT load_id, 'REDEMPTION', source_file, txn_no, txn_id, 'MANDATORY', 'ERROR', 'txn_id', NULL, 'txn_id is required'
  FROM V_RAW_REDEMPTION_FLAT WHERE load_id = '${LOAD_ID}' AND bad_txn_id
UNION ALL
SELECT load_id, 'REDEMPTION', source_file, txn_no, txn_id, 'MANDATORY', 'ERROR', 'member_id', NULL, 'member_id is required'
  FROM V_RAW_REDEMPTION_FLAT WHERE load_id = '${LOAD_ID}' AND bad_member
UNION ALL
SELECT load_id, 'REDEMPTION', source_file, txn_no, txn_id, 'BAD_DATE', 'ERROR', 'txn_date', txn_date_raw,
       'txn_date missing or invalid'
  FROM V_RAW_REDEMPTION_FLAT WHERE load_id = '${LOAD_ID}' AND bad_date
UNION ALL
SELECT load_id, 'REDEMPTION', source_file, txn_no, txn_id, 'MILES', 'ERROR', 'miles_redeemed', TO_VARCHAR(miles_v),
       'miles_redeemed must be a positive integer'
  FROM V_RAW_REDEMPTION_FLAT WHERE load_id = '${LOAD_ID}' AND bad_miles
UNION ALL
SELECT load_id, 'REDEMPTION', source_file, txn_no, txn_id, 'DATE_ORDER', 'WARNING', 'txn_date', txn_date_raw,
       'txn_date is after feed_date'
  FROM V_RAW_REDEMPTION_FLAT WHERE load_id = '${LOAD_ID}' AND txn_after_feed
UNION ALL
SELECT load_id, 'REDEMPTION', source_file, txn_no, txn_id, 'STATUS', 'WARNING', 'status', status_c,
       'status not in the allowed list'
  FROM V_RAW_REDEMPTION_FLAT WHERE load_id = '${LOAD_ID}' AND bad_status AND NOT bad_txn_id
UNION ALL
SELECT r.load_id, 'REDEMPTION', r.source_file, 1, NULL, 'JSON_SHAPE', 'ERROR', 'redemptions', NULL,
       'redemptions must be an array'
  FROM RAW_REDEMPTION_FEED r WHERE r.load_id = '${LOAD_ID}' AND NOT IS_ARRAY(r.doc:redemptions);

-- ---------------------------------------------------------------------------
-- 3. Merge on txn_id: a later feed can move PENDING -> COMPLETED; an older replay
--    never reverts it (same rule as RedemptionStore.merge). member_key is cleared on
--    update and re-resolved in step 4.
-- ---------------------------------------------------------------------------
MERGE INTO REDEMPTION_FACT t
USING (
    SELECT txn_id, source_system, member_id, txn_d, partner,
           TRY_TO_NUMBER(miles_v::VARCHAR) AS miles_redeemed, status_c, feed_d, source_file, load_id
      FROM V_RAW_REDEMPTION_FLAT
     WHERE load_id = '${LOAD_ID}' AND is_valid
    QUALIFY ROW_NUMBER() OVER (PARTITION BY txn_id
                               ORDER BY feed_d DESC NULLS LAST, source_file DESC, txn_no DESC) = 1
) s
   ON t.txn_id = s.txn_id
WHEN MATCHED AND (t.feed_date IS NULL OR s.feed_d >= t.feed_date) THEN UPDATE SET
    source_system = s.source_system, member_id = s.member_id, member_key = NULL,
    txn_date = s.txn_d, partner = s.partner, miles_redeemed = s.miles_redeemed,
    status = s.status_c, feed_date = s.feed_d, source_file = s.source_file, load_id = s.load_id,
    updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT
    (txn_id, source_system, member_id, member_key, txn_date, partner, miles_redeemed, status,
     feed_date, source_file, load_id)
VALUES
    (s.txn_id, s.source_system, s.member_id, NULL, s.txn_d, s.partner, s.miles_redeemed, s.status_c,
     s.feed_d, s.source_file, s.load_id);

-- ---------------------------------------------------------------------------
-- 4. Link facts to members. Touches only unresolved facts, so it also picks up older
--    orphans whose member arrived in this load. Safe to re-run.
-- ---------------------------------------------------------------------------
UPDATE REDEMPTION_FACT f
   SET member_key = i.member_key, updated_at = CURRENT_TIMESTAMP()
  FROM MEMBER_INDEX i
 WHERE f.member_key IS NULL
   AND i.member_key = f.source_system || '|' || f.member_id;

-- ---------------------------------------------------------------------------
-- 5. Read surface. Requires the MEMBER_ALL view (generated by the runner from DIM_COUNTRY).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW REDEMPTION_WITH_MEMBER AS
SELECT rf.txn_id, rf.txn_date, rf.partner, rf.miles_redeemed, rf.status, rf.feed_date,
       rf.source_system, rf.member_id, rf.member_key,
       m.country_code,                      -- CURRENT country, looked up live
       m.member_name, m.tier_code, m.record_status
  FROM REDEMPTION_FACT rf
  LEFT JOIN MEMBER_ALL m ON m.member_key = rf.member_key;

CREATE OR REPLACE VIEW V_REDEMPTION_ORPHANS AS
SELECT txn_id, source_system, member_id, txn_date, miles_redeemed, status, feed_date
  FROM REDEMPTION_FACT
 WHERE member_key IS NULL;

-- Example: miles redeemed per current country, completed, last 30 days
--   SELECT country_code, SUM(miles_redeemed) FROM REDEMPTION_WITH_MEMBER
--    WHERE status = 'COMPLETED' AND txn_date >= DATEADD('day', -30, CURRENT_DATE()) GROUP BY 1;
