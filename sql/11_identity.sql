-- 11_identity.sql  (Snowflake, v2)
-- Weakness #3: the composite key (source|id) treats one real person who appears in two
-- sources as two members. This view PROPOSES cross-source matches for a person to confirm
-- into MEMBER_XREF. It never merges anything: a wrong merge (two people fused) is worse
-- than a missed one (one person listed twice).
--   blocking key : normalised name + date of birth
--   only pairs from DIFFERENT source systems (inside one source the id is authoritative)
--   HIGH = same enrollment date as well; MEDIUM = name + dob only
-- Normalisation is ASCII-only (accents dropped) and identical to identity.norm_name().
-- Very common name+dob blocks would explode the self-join; the Python version caps a
-- block at 20 members. At production scale add the same cap with a COUNT(*) OVER block.
USE SCHEMA SKYPOINTS.CORE;

CREATE OR REPLACE VIEW V_MEMBER_MATCH_CANDIDATES AS
WITH m AS (
    SELECT member_key, source_system, enrollment_date, dob,
           TRIM(REGEXP_REPLACE(LOWER(member_name), '[^a-z0-9]+', ' ')) AS name_norm
      FROM MEMBER_ALL
     WHERE dob IS NOT NULL
)
SELECT a.member_key AS member_key_a,
       b.member_key AS member_key_b,
       a.name_norm,
       a.dob,
       IFF(COALESCE(a.enrollment_date = b.enrollment_date, FALSE), 'HIGH', 'MEDIUM') AS confidence
  FROM m a
  JOIN m b
    ON a.name_norm = b.name_norm
   AND a.dob = b.dob
   AND a.member_key < b.member_key
   AND a.source_system <> b.source_system
 WHERE a.name_norm <> '';

-- Confirm a match (human decision):
--   INSERT INTO MEMBER_XREF (golden_id, member_key, status, confirmed_by)
--   VALUES ('G-0001','IND|7','CONFIRMED','zeeshan'), ('G-0001','GLOBAL|223999','CONFIRMED','zeeshan');
