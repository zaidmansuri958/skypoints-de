from datetime import date

from skypoints.readers import RawRow
from skypoints.stage import ERROR, WARNING, stage_row

AS_OF = date(2026, 11, 21)


def raw(system="GLOBAL", **v):
    base = {
        "member_name": "Elena", "member_id": "223457", "enrollment_date": "20101012",
        "last_flight_date": "20121013", "tier_code": "GLD", "agent_name": "Sam", "state": "CA",
        "country": "USA", "dob": "03051985", "is_active": "A",
    }
    base.update(v)
    return RawRow(system, "f.txt", 1, date(2024, 1, 15), base)


def rules(issues, severity=None):
    return {i.rule_code for i in issues if severity is None or i.severity == severity}


def test_clean_row_stages_with_derived_columns():
    m, issues = stage_row(raw(), AS_OF)
    assert issues == []
    assert m.member_key == "GLOBAL|223457"
    assert m.country_code == "USA"
    assert m.dob == date(1985, 5, 3) and m.age == 41
    assert m.stale_member is True


def test_recent_flight_not_stale():
    m, _ = stage_row(raw(last_flight_date="20261101"), AS_OF)
    assert m.stale_member is False


def test_mandatory_fields_reject_row():
    for col, rule in [("member_name", "MANDATORY"), ("member_id", "MANDATORY"), ("enrollment_date", "MANDATORY")]:
        m, issues = stage_row(raw(**{col: None}), AS_OF)
        assert m is None and rule in rules(issues, ERROR), col


def test_blank_and_null_token_count_as_missing():
    m, issues = stage_row(raw(member_name="   "), AS_OF)
    assert m is None and "MANDATORY" in rules(issues, ERROR)


def test_invalid_enrollment_date_rejects_row_aus_sample():
    r = raw(system="AUS", member_name="Jonnathan", member_id="2", enrollment_date="2021-13-13",
            last_flight_date="2022-01-05", dob="1997-12-13", country=None)
    m, issues = stage_row(r, AS_OF)
    assert m is None
    assert any(i.rule_code == "BAD_DATE" and i.column_name == "enrollment_date" for i in issues)


def test_null_dob_is_allowed():
    r = raw(system="AUS", member_id="1", enrollment_date="2022-05-11", last_flight_date="2022-08-01",
            dob="NULL", country=None)
    m, issues = stage_row(r, AS_OF)
    assert m is not None and m.dob is None and m.age is None and issues == []


def test_bad_dob_is_warning_row_kept():
    m, issues = stage_row(raw(dob="99999999"), AS_OF)
    assert m is not None and m.dob is None
    assert "BAD_DATE" in rules(issues, WARNING)


def test_country_alias_normalized_with_warning():
    m, issues = stage_row(raw(country="PHIL"), AS_OF)
    assert m.country_code == "PHL" and "COUNTRY_ALIAS" in rules(issues, WARNING)
    m, _ = stage_row(raw(country="AU"), AS_OF)
    assert m.country_code == "AUS"


def test_unknown_country_rejected():
    m, issues = stage_row(raw(country="ZZZ"), AS_OF)
    assert m is None and "UNKNOWN_COUNTRY" in rules(issues, ERROR)


def test_per_country_file_uses_filename_country():
    r = raw(system="IND", member_id="1", enrollment_date="1/1/2022", last_flight_date="6/15/2022",
            dob="12/1/1998", country=None)
    m, _ = stage_row(r, AS_OF)
    assert m.country_code == "IND" and m.member_key == "IND|1"


def test_usa_compact_dates():
    r = raw(system="USA", member_id="2", enrollment_date="1052022", last_flight_date="1152022",
            dob=None, country=None)
    m, _ = stage_row(r, AS_OF)
    assert m.enrollment_date == date(2022, 1, 5) and m.last_flight_date == date(2022, 1, 15)


def test_future_enrollment_rejected():
    m, issues = stage_row(raw(enrollment_date="20271231"), AS_OF)
    assert m is None and "DATE_ORDER" in rules(issues, ERROR)


def test_flight_before_enrollment_warns():
    m, issues = stage_row(raw(enrollment_date="20200101", last_flight_date="20190101"), AS_OF)
    assert m is not None and "DATE_ORDER" in rules(issues, WARNING)


def test_age_out_of_range_warns():
    m, issues = stage_row(raw(dob="01011800"), AS_OF)
    assert "AGE_RANGE" in rules(issues, WARNING)


def test_tier_and_active_domains_warn():
    _, issues = stage_row(raw(tier_code="XXX", is_active="Z"), AS_OF)
    assert {"TIER_CODE", "ACTIVE_FLAG"} <= rules(issues, WARNING)


def test_non_numeric_member_id_warns():
    m, issues = stage_row(raw(member_id="AB12"), AS_OF)
    assert m is not None and "ID_FORMAT" in rules(issues, WARNING)


def test_pipe_row_shape_error_rejects():
    r = raw()
    r.shape_error = "expected 11 fields, got 10"
    m, issues = stage_row(r, AS_OF)
    assert m is None and "ROW_SHAPE" in rules(issues, ERROR)


def test_over_length_values_warn_but_row_is_kept():
    m, issues = stage_row(raw(tier_code="PLATINUM", state="QUEENSLAND", member_id="1" * 19), AS_OF)
    assert m is not None
    cols = {i.column_name for i in issues if i.rule_code == "FIELD_LENGTH"}
    assert cols == {"tier_code", "state", "member_id"}
    assert all(i.severity == WARNING for i in issues if i.rule_code == "FIELD_LENGTH")


def test_post_code_longer_than_five_digits_warns_and_is_dropped():
    m, issues = stage_row(raw(post_code="123456"), AS_OF)
    assert m.post_code is None and "POST_CODE" in rules(issues, WARNING)
    m, issues = stage_row(raw(post_code="01234"), AS_OF)
    assert m.post_code == 1234 and "POST_CODE" not in rules(issues)
