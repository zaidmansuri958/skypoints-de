from datetime import date

import pytest

from skypoints.normalize import BAD, EMPTY, OK, compute_age, is_stale, normalize_country, parse_date

AS_OF = date(2026, 11, 21)


@pytest.mark.parametrize("value,fmt,expected", [
    ("1/1/2022", "MDY_SLASH", date(2022, 1, 1)),
    ("8/13/1952", "MDY_SLASH", date(1952, 8, 13)),
    ("6152022", "MDY_COMPACT", date(2022, 6, 15)),     # leading zero lost
    ("1052022", "MDY_COMPACT", date(2022, 1, 5)),
    ("12282021", "MDY_COMPACT", date(2021, 12, 28)),
    ("2022-05-11", "ISO", date(2022, 5, 11)),
    ("20101012", "YMD_COMPACT", date(2010, 10, 12)),
    ("3051985", "DMY_COMPACT", date(1985, 5, 3)),
    ("03051985", "DMY_COMPACT", date(1985, 5, 3)),
])
def test_parse_valid(value, fmt, expected):
    assert parse_date(value, fmt) == (expected, OK)


@pytest.mark.parametrize("value,fmt", [
    ("2021-13-13", "ISO"),          # month 13 (AUS sample)
    ("13/40/2022", "MDY_SLASH"),
    ("1332022", "MDY_COMPACT"),     # pads to 01332022 -> day 33
    ("12345", "MDY_COMPACT"),       # too short
    ("2010101", "YMD_COMPACT"),     # 7 digits
    ("abc", "ISO"),
])
def test_parse_bad(value, fmt):
    assert parse_date(value, fmt) == (None, BAD)


@pytest.mark.parametrize("value", [None, "", "  ", "NULL", "null"])
def test_parse_empty(value):
    assert parse_date(value, "ISO") == (None, EMPTY)


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        parse_date("2022-01-01", "NOPE")


@pytest.mark.parametrize("raw,code,aliased", [
    ("USA", "USA", False), ("AU", "AUS", True), ("phil", "PHL", True),
    (" IND ", "IND", False), ("XX", None, False), (None, None, False),
])
def test_country(raw, code, aliased):
    assert normalize_country(raw) == (code, aliased)


def test_age_birthday_boundaries():
    assert compute_age(date(1985, 5, 3), AS_OF) == 41
    assert compute_age(date(1985, 11, 21), AS_OF) == 41      # birthday today
    assert compute_age(date(1985, 11, 22), AS_OF) == 40      # birthday tomorrow
    assert compute_age(None, AS_OF) is None


def test_age_leap_day():
    assert compute_age(date(2000, 2, 29), date(2027, 2, 28)) == 26
    assert compute_age(date(2000, 2, 29), date(2027, 3, 1)) == 27


def test_stale_boundary():
    assert is_stale(date(2026, 8, 23), AS_OF) is False   # exactly 90 days
    assert is_stale(date(2026, 8, 22), AS_OF) is True    # 91 days
    assert is_stale(None, AS_OF) is True                 # never flown
