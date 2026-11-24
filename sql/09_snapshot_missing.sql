-- 09_snapshot_missing.sql  (Snowflake, v2)
-- Weakness #2: members that a FULL-SNAPSHOT feed no longer lists are soft-deleted
-- (record_status = 'MISSING_FROM_FEED'), never left ACTIVE and never hard-deleted.
-- Only runs for sources with feed_mode = 'snapshot' (DIM_SOURCE / config.FEED_MODE).
-- Template variables: ${LOAD_ID} ${SOURCE_SYSTEM} ${TARGET_TABLE} ${FEED_DATE}
-- Run once per country table, AFTER the merge, so members that reappear in this very
-- load have already been upserted as ACTIVE.
USE SCHEMA SKYPOINTS.CORE;

-- "Seen" means present in the raw file even if the row failed validation:
-- a malformed row is not a departed member.
UPDATE ${TARGET_TABLE} t
   SET record_status = 'MISSING_FROM_FEED',
       missing_since = DATE '${FEED_DATE}',
       updated_at    = CURRENT_TIMESTAMP()
 WHERE t.source_system = '${SOURCE_SYSTEM}'
   AND t.record_status = 'ACTIVE'
   AND NOT EXISTS (
        SELECT 1 FROM RAW_MEMBER_FILE r
         WHERE r.load_id = '${LOAD_ID}'
           AND r.source_system = '${SOURCE_SYSTEM}'
           AND r.source_system || '|' || CLEAN_TXT(r.member_id) = t.member_key);
