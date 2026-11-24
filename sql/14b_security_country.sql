-- 14b_security_country.sql  (Snowflake, v2)
-- Applied to each per-country table by `snowflake_runner security`.
-- Template variables: ${TARGET_TABLE}
-- `age` is deliberately NOT masked (analytics needs it) although it narrows a date of birth
-- to a year; mask or band it too if that is too revealing for your analysts.
USE SCHEMA SKYPOINTS.CORE;

ALTER TABLE ${TARGET_TABLE} MODIFY COLUMN dob         SET MASKING POLICY MP_PII_DATE FORCE;
ALTER TABLE ${TARGET_TABLE} MODIFY COLUMN member_name SET MASKING POLICY MP_PII_TEXT FORCE;
ALTER TABLE ${TARGET_TABLE} MODIFY COLUMN dob         SET TAG PII_CLASS = 'quasi';
ALTER TABLE ${TARGET_TABLE} MODIFY COLUMN member_name SET TAG PII_CLASS = 'direct';

-- Best effort: DROP fails harmlessly when the policy is not attached yet (first run); the
-- runner ignores exactly that error so the policy can be re-applied idempotently.
ALTER TABLE ${TARGET_TABLE} DROP ROW ACCESS POLICY RAP_MEMBER_COUNTRY;
ALTER TABLE ${TARGET_TABLE} ADD ROW ACCESS POLICY RAP_MEMBER_COUNTRY ON (country_code);

GRANT SELECT ON TABLE ${TARGET_TABLE} TO ROLE SKYPOINTS_ANALYST;
GRANT SELECT ON TABLE ${TARGET_TABLE} TO ROLE SKYPOINTS_PII_READER;
