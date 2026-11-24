-- 09a_snapshot_guard.sql  (Snowflake, v2)
-- Guard run BEFORE 09_snapshot_missing.sql. The runner refuses to mark anyone missing
-- when seen_cnt is empty or below (1 - SNAPSHOT_DROP_GUARD) * active_cnt, because that
-- pattern means a truncated file, not a mass departure.
-- Template variables: ${LOAD_ID} ${SOURCE_SYSTEM}
USE SCHEMA SKYPOINTS.CORE;

SELECT
    (SELECT COUNT(DISTINCT CLEAN_TXT(member_id)) FROM RAW_MEMBER_FILE
      WHERE load_id = '${LOAD_ID}' AND source_system = '${SOURCE_SYSTEM}'
        AND CLEAN_TXT(member_id) IS NOT NULL)                              AS seen_cnt,
    (SELECT COUNT(*) FROM MEMBER_ALL
      WHERE source_system = '${SOURCE_SYSTEM}' AND record_status = 'ACTIVE') AS active_cnt;
