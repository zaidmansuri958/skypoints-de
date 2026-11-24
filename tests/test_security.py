import re

import pytest

from skypoints.config import COUNTRIES
from skypoints.snowflake_runner import SnowflakeRunner
from skypoints.sqlrender import statements

from test_runner import Fake, Log


def make(fail_on=None):
    log = Log()
    return SnowflakeRunner(Fake(log, {}, "main", fail_on), log=lambda m: None), log


def test_security_file_defines_roles_policies_and_least_privilege_grants():
    sql = "\n".join(statements("14_security.sql"))
    for role in ("SKYPOINTS_ADMIN", "SKYPOINTS_PII_READER", "SKYPOINTS_ANALYST"):
        assert f"CREATE ROLE IF NOT EXISTS {role}" in sql
    for policy in ("MP_PII_DATE", "MP_PII_TEXT", "RAP_MEMBER_COUNTRY"):
        assert policy in sql
    # analysts never get the raw, staging or reject tables (they hold unmasked values)
    analyst = [s for s in statements("14_security.sql") if s.startswith("GRANT") and "SKYPOINTS_ANALYST" in s]
    assert analyst and not any(re.search(r"RAW_|STG_|PIPELINE_RUN_LOG|MEMBER_XREF", s) for s in analyst)
    # the name-matching view is PII-reader only: masked names would all "match" each other
    match_grants = [s for s in statements("14_security.sql") if "V_MEMBER_MATCH_CANDIDATES" in s and s.startswith("GRANT")]
    assert len(match_grants) == 1 and "SKYPOINTS_PII_READER" in match_grants[0]


def test_masking_policies_let_only_privileged_roles_see_values():
    sql = "\n".join(statements("14_security.sql"))
    for body in re.findall(r"CREATE MASKING POLICY.*?(?=CREATE |ALTER |GRANT |$)", sql, re.S):
        assert "SKYPOINTS_PII_READER" in body and "SKYPOINTS_ADMIN" in body and "ELSE" in body


def test_row_policy_admin_sees_all_others_only_mapped_countries():
    sql = next(s for s in statements("14_security.sql") if "CREATE ROW ACCESS POLICY" in s)
    assert "SKYPOINTS_ADMIN" in sql and "ROLE_COUNTRY_ACCESS" in sql and "IS_ROLE_IN_SESSION(r.role_name)" in sql


def test_security_is_applied_to_every_country_table_and_masks_name_and_dob():
    r, log = make()
    r.security()
    for table in COUNTRIES.values():
        masks = log.all(rf"^ALTER TABLE {table} MODIFY COLUMN (dob|member_name)\s+SET MASKING POLICY")
        assert len(masks) == 2, table
        assert log.any(rf"^ALTER TABLE {table} ADD ROW ACCESS POLICY RAP_MEMBER_COUNTRY ON \(country_code\)")
        assert log.any(rf"^ALTER TABLE {table} MODIFY COLUMN member_name\s+SET TAG PII_CLASS = 'direct'")
    assert log.first(r"CREATE ROW ACCESS POLICY") < log.first(r"^ALTER TABLE MEMBER_IND MODIFY COLUMN dob")
    # drop-then-add per table, so a re-run replaces the policy instead of failing on "already attached"
    assert log.first(r"^ALTER TABLE MEMBER_IND DROP ROW ACCESS POLICY") < log.first(r"^ALTER TABLE MEMBER_IND ADD ROW ACCESS POLICY")


def test_security_tolerates_the_expected_rerun_errors():
    # first run: DROP finds nothing attached; re-run: history/index already have the policy attached
    r, log = make(fail_on=r"DROP ROW ACCESS POLICY|ALTER TABLE MEMBER_(HISTORY|INDEX) ADD ROW ACCESS POLICY")
    r.security()                                                  # must not raise
    assert log.any(r"^ALTER TABLE MEMBER_PHL ADD ROW ACCESS POLICY")


def test_a_failed_add_on_a_country_table_is_a_real_error():
    r, _ = make(fail_on=r"^ALTER TABLE MEMBER_CAN ADD ROW ACCESS POLICY")
    with pytest.raises(RuntimeError):
        r.security()


def test_security_does_not_swallow_unrelated_errors():
    r, _ = make(fail_on=r"CREATE ROLE IF NOT EXISTS SKYPOINTS_ADMIN")
    with pytest.raises(RuntimeError):
        r.security()
    r, _ = make(fail_on=r"SET MASKING POLICY MP_PII_DATE")
    with pytest.raises(RuntimeError):
        r.security()


def test_purge_removes_pii_bearing_tables_after_the_retention_window():
    r, log = make()
    r.purge(30)
    for table in ("RAW_MEMBER_FILE", "RAW_REDEMPTION_FEED", "STG_REJECT", "STG_MEMBER"):
        assert log.any(rf"^DELETE FROM {table}\s+WHERE \w+\s+< DATEADD\('day', -30, CURRENT_TIMESTAMP\(\)\)")
    assert not log.any(r"DELETE FROM MEMBER_")                    # curated data is never purged


@pytest.mark.parametrize("days", [0, -5])
def test_purge_refuses_a_non_positive_window(days):
    r, log = make()
    with pytest.raises(ValueError):
        r.purge(days)
    assert log.stmts == []
