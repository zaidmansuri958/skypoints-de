-- 16_retention.sql  (Snowflake, v2)
-- RAW and the reject table hold UNMASKED source values (including bad values, which can be
-- personal data). They exist for replay and debugging, not as a long-term store, so purge them
-- after ${RETENTION_DAYS} days: `snowflake_runner purge --days 30`.
-- Template variables: ${RETENTION_DAYS}
USE SCHEMA SKYPOINTS.CORE;

DELETE FROM RAW_MEMBER_FILE     WHERE loaded_at   < DATEADD('day', -${RETENTION_DAYS}, CURRENT_TIMESTAMP());
DELETE FROM RAW_REDEMPTION_FEED WHERE loaded_at   < DATEADD('day', -${RETENTION_DAYS}, CURRENT_TIMESTAMP());
DELETE FROM STG_REJECT          WHERE rejected_at < DATEADD('day', -${RETENTION_DAYS}, CURRENT_TIMESTAMP());
DELETE FROM STG_MEMBER          WHERE loaded_at   < DATEADD('day', -${RETENTION_DAYS}, CURRENT_TIMESTAMP());
DELETE FROM STG_APPLY           WHERE load_id NOT IN (SELECT DISTINCT load_id FROM STG_MEMBER);
