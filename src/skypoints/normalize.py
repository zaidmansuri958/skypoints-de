"""Pure normalization helpers: dates, countries, derived columns."""
from __future__ import annotations

from datetime import date, datetime

from .config import COUNTRY_ALIASES, STALE_DAYS

NULL_TOKENS = frozenset({"", "NULL", "NONE", "N/A", "NA"})

OK, EMPTY, BAD = "ok", "empty", "bad"


def clean(value: object) -> str | None:
    """Trim; map empty/'NULL' tokens to None."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s.upper() in NULL_TOKENS else s


def parse_date(value: object, fmt: str) -> tuple[date | None, str]:
    """Parse ``value`` using a named source format.

    Returns (date, status) where status is 'ok', 'empty' (null-like input) or
    'bad' (present but unparseable/invalid, e.g. month 13).
    """
    s = clean(value)
    if s is None:
        return None, EMPTY
    try:
        if fmt == "MDY_SLASH":
            return datetime.strptime(s, "%m/%d/%Y").date(), OK
        if fmt == "ISO":
            return datetime.strptime(s, "%Y-%m-%d").date(), OK
        if fmt == "YMD_COMPACT":
            if not (s.isdigit() and len(s) == 8):
                return None, BAD
            return datetime.strptime(s, "%Y%m%d").date(), OK
        if fmt in ("MDY_COMPACT", "DMY_COMPACT"):
            # Excel/CSV exports drop the leading zero (6152022 -> 06152022).
            if not (s.isdigit() and 7 <= len(s) <= 8):
                return None, BAD
            s = s.zfill(8)
            return datetime.strptime(s, "%m%d%Y" if fmt == "MDY_COMPACT" else "%d%m%Y").date(), OK
    except ValueError:
        return None, BAD
    raise ValueError(f"unknown date format: {fmt}")


def normalize_country(value: object) -> tuple[str | None, bool]:
    """Return (ISO alpha-3 or None, was_alias). was_alias is True if the input differed from the code."""
    s = clean(value)
    if s is None:
        return None, False
    code = COUNTRY_ALIASES.get(s.upper())
    if code is None:
        return None, False
    return code, s.upper() != code


def compute_age(dob: date | None, as_of: date) -> int | None:
    """Completed years as of ``as_of``. Matches the SQL MMDD comparison (Feb 29 -> Mar 1 in common years)."""
    if dob is None:
        return None
    return as_of.year - dob.year - (1 if (as_of.month, as_of.day) < (dob.month, dob.day) else 0)


def is_stale(last_flight: date | None, as_of: date, days: int = STALE_DAYS) -> bool:
    """True when more than ``days`` days since the last flight. Never-flown counts as stale."""
    if last_flight is None:
        return True
    return (as_of - last_flight).days > days
