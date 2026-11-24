-- 07b_upsert.sql  (Snowflake, v2)
-- Phase 2 of the country split, run once per country table (in parallel): upsert the winners
-- whose country is ${COUNTRY_CODE}. MERGE keeps a re-run idempotent. A member that was
-- soft-deleted (MISSING_FROM_FEED) and reappears is reactivated here.
-- Template variables: ${LOAD_ID} ${COUNTRY_CODE} ${TARGET_TABLE}
USE SCHEMA SKYPOINTS.CORE;

MERGE INTO ${TARGET_TABLE} t
USING (SELECT * FROM STG_APPLY WHERE load_id = '${LOAD_ID}' AND country_code = '${COUNTRY_CODE}') s
   ON t.member_key = s.member_key
WHEN MATCHED THEN UPDATE SET
    source_system = s.source_system, source_member_id = s.source_member_id, member_name = s.member_name,
    enrollment_date = s.enrollment_date, last_flight_date = s.last_flight_date, tier_code = s.tier_code,
    agent_name = s.agent_name, state = s.state, country_code = s.country_code, post_code = s.post_code,
    dob = s.dob, is_active = s.is_active, member_type = s.member_type,
    as_of_date = s.as_of_date, age = s.age, stale_member = s.stale_member,
    record_status = 'ACTIVE', last_seen_date = COALESCE(s.feed_date, s.as_of_date), missing_since = NULL,
    feed_date = s.feed_date, source_file = s.source_file, load_id = s.load_id,
    updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
    member_key, source_system, source_member_id, member_name, enrollment_date, last_flight_date,
    tier_code, agent_name, state, country_code, post_code, dob, is_active, member_type,
    as_of_date, age, stale_member, record_status, last_seen_date, missing_since,
    feed_date, source_file, load_id)
VALUES (
    s.member_key, s.source_system, s.source_member_id, s.member_name, s.enrollment_date, s.last_flight_date,
    s.tier_code, s.agent_name, s.state, s.country_code, s.post_code, s.dob, s.is_active, s.member_type,
    s.as_of_date, s.age, s.stale_member, 'ACTIVE', COALESCE(s.feed_date, s.as_of_date), NULL,
    s.feed_date, s.source_file, s.load_id);
