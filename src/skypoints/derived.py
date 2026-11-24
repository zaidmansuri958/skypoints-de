"""Incremental refresh of the time-dependent derived columns (age, stale_member).

Both change on predictable days: age on the member's birthday, stale_member on
last_flight_date + (STALE_DAYS + 1). So a daily job only needs to touch the few rows whose
trigger date fell inside (watermark, as_of]; it does not rescan the whole base. The SQL
(sql/13_refresh_derived.sql) is generated from the same window logic (window_mmdds), and
the refresh is an idempotent recompute, so a window that is a superset of the truth is safe.
"""
from __future__ import annotations

from datetime import date, timedelta

from .config import STALE_DAYS
from .normalize import compute_age, is_stale
from .transform import CountryStore

FULL_REFRESH_AFTER_DAYS = 366


def window_mmdds(watermark: date, as_of: date) -> set[tuple[int, int]] | None:
    """(month, day) pairs whose birthday fell in (watermark, as_of]; None = refresh everyone.

    A Feb 29 birthday takes effect on Mar 1 in common years, so Feb 29 is included
    whenever Mar 1 is in the window (a harmless superset: recompute is idempotent).
    """
    days = (as_of - watermark).days
    if days <= 0:
        return set()
    if days > FULL_REFRESH_AFTER_DAYS:
        return None
    pairs = set()
    d = watermark
    for _ in range(days):
        d += timedelta(days=1)
        pairs.add((d.month, d.day))
    if (3, 1) in pairs:
        pairs.add((2, 29))
    return pairs


def stale_flip_range(watermark: date, as_of: date, stale_days: int = STALE_DAYS) -> tuple[date, date]:
    """last_flight_date values that became stale inside (watermark, as_of] (inclusive range)."""
    return watermark - timedelta(days=stale_days), as_of - timedelta(days=stale_days + 1)


def refresh_derived(store: CountryStore, as_of: date, stale_days: int = STALE_DAYS) -> int:
    """Bring age/stale_member current through ``as_of`` touching only affected members.

    Returns the number of members recomputed and advances store.derived_watermark.
    First run (no watermark) only sets it: members staged on a run are already current.
    """
    wm = store.derived_watermark
    if wm is None:
        store.derived_watermark = as_of
        return 0
    if as_of < wm:
        raise ValueError(f"as-of date {as_of} is before the derived watermark {wm}; "
                         "derived columns cannot move backwards")
    if as_of == wm:
        return 0
    pairs = window_mmdds(wm, as_of)
    lo, hi = stale_flip_range(wm, as_of, stale_days)
    touched = 0
    for table in store.tables.values():
        for m in table.values():
            birthday = m.dob is not None and (pairs is None or (m.dob.month, m.dob.day) in pairs)
            flipped = m.last_flight_date is not None and lo <= m.last_flight_date <= hi
            if pairs is None or birthday or flipped:
                m.age = compute_age(m.dob, as_of)
                m.stale_member = is_stale(m.last_flight_date, as_of, stale_days)
                m.as_of_date = as_of
                touched += 1
    store.derived_watermark = as_of
    return touched


def recompute_all(store: CountryStore, as_of: date, stale_days: int = STALE_DAYS) -> None:
    """Reference full recompute, used by tests to prove the incremental path is exact."""
    for table in store.tables.values():
        for m in table.values():
            m.age = compute_age(m.dob, as_of)
            m.stale_member = is_stale(m.last_flight_date, as_of, stale_days)
            m.as_of_date = as_of


def sql_window_vars(watermark: date, as_of: date, stale_days: int = STALE_DAYS) -> dict[str, str]:
    """Template variables for sql/13_refresh_derived.sql, built from the same window logic
    the Python refresh uses, so the SQL and Python predicates cannot drift apart."""
    pairs = window_mmdds(watermark, as_of)
    lo, hi = stale_flip_range(watermark, as_of, stale_days)
    if pairs is None:
        birthday, refresh_all = "FALSE", "TRUE"
    elif not pairs:
        birthday, refresh_all = "FALSE", "FALSE"
    else:
        listed = ",".join(f"'{m:02d}{d:02d}'" for m, d in sorted(pairs))
        birthday, refresh_all = f"TO_CHAR(t.dob,'MMDD') IN ({listed})", "FALSE"
    return {
        "FROM_DATE": watermark.isoformat(), "AS_OF": as_of.isoformat(),
        "STALE_DAYS": str(stale_days), "REFRESH_ALL": refresh_all, "BIRTHDAY_FILTER": birthday,
        "FLIP_FROM": lo.isoformat(), "FLIP_TO": hi.isoformat(),
    }
