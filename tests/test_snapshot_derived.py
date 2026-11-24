import copy
import random
from datetime import date, timedelta

import pytest

from skypoints.derived import recompute_all, refresh_derived, window_mmdds
from skypoints.readers import RawRow
from skypoints.stage import stage_row
from skypoints.transform import CountryStore, latest_wins

AS_OF = date(2026, 11, 21)


def make(mid, system="GLOBAL", country="USA", feed=date(2024, 1, 1), dob=date(1985, 5, 3),
         flight=date(2024, 1, 1), as_of=AS_OF, row=1):
    v = {"member_name": f"M{mid}", "member_id": str(mid), "enrollment_date": "20100101",
         "last_flight_date": flight.strftime("%Y%m%d") if flight else None, "tier_code": "GLD",
         "country": country, "dob": dob.strftime("%d%m%Y") if dob else None, "is_active": "A"}
    if system != "GLOBAL":
        raise AssertionError("helper only builds GLOBAL rows")
    m, issues = stage_row(RawRow(system, "f.txt", row, feed, v), as_of)
    assert m is not None, issues
    return m


def load(store, *ms):
    store.apply(latest_wins(ms))


# ---------------------------------------------------------------- snapshot / missing members

def test_member_absent_from_snapshot_is_soft_deleted():
    s = CountryStore()
    load(s, make(1), make(2, feed=date(2024, 1, 1)), make(3))
    marked, why = s.mark_missing("GLOBAL", {"GLOBAL|1", "GLOBAL|3"}, date(2024, 2, 1), guard=0.5)
    assert (marked, why) == (1, None)
    m = s.tables["USA"]["GLOBAL|2"]
    assert m.record_status == "MISSING_FROM_FEED" and m.missing_since == date(2024, 2, 1)
    assert s.tables["USA"]["GLOBAL|1"].record_status == "ACTIVE"
    assert "GLOBAL|2" in s.tables["USA"]            # soft delete: the row is kept


def test_returning_member_becomes_active_again():
    s = CountryStore()
    load(s, make(1), make(2))
    s.mark_missing("GLOBAL", {"GLOBAL|1"}, date(2024, 2, 1), guard=0.9)
    load(s, make(2, feed=date(2024, 3, 1)))
    m = s.tables["USA"]["GLOBAL|2"]
    assert m.record_status == "ACTIVE" and m.missing_since is None
    assert m.last_seen_date == date(2024, 3, 1)


def test_snapshot_only_touches_its_own_source_system():
    s = CountryStore()
    load(s, make(1))
    ind = stage_row(RawRow("IND", "IND.csv", 1, None, {
        "member_name": "Vikas", "member_id": "1", "enrollment_date": "1/1/2022",
        "last_flight_date": "6/15/2022", "tier_code": "SLV", "dob": "12/1/1998"}), AS_OF)[0]
    load(s, ind)
    s.mark_missing("GLOBAL", {"GLOBAL|999"}, date(2024, 2, 1), guard=1.0)
    assert s.tables["IND"]["IND|1"].record_status == "ACTIVE"


def test_truncated_snapshot_is_refused_by_guard():
    s = CountryStore()
    load(s, *[make(i, row=i) for i in range(1, 11)])
    marked, why = s.mark_missing("GLOBAL", {"GLOBAL|1", "GLOBAL|2"}, date(2024, 2, 1), guard=0.20)
    assert marked == 0 and "partial file" in why
    assert all(m.record_status == "ACTIVE" for m in s.tables["USA"].values())


def test_empty_snapshot_never_marks_everyone_missing():
    s = CountryStore()
    load(s, make(1))
    marked, why = s.mark_missing("GLOBAL", set(), date(2024, 2, 1), guard=0.2)
    assert marked == 0 and why


def test_mark_missing_is_idempotent_and_keeps_first_missing_date():
    s = CountryStore()
    load(s, make(1), make(2))
    s.mark_missing("GLOBAL", {"GLOBAL|1"}, date(2024, 2, 1), guard=0.9)
    marked, _ = s.mark_missing("GLOBAL", {"GLOBAL|1"}, date(2024, 3, 1), guard=0.9)
    assert marked == 0                                        # already missing, not re-marked
    assert s.tables["USA"]["GLOBAL|2"].missing_since == date(2024, 2, 1)


# ---------------------------------------------------------------- incremental derived columns

def test_window_includes_feb29_when_mar1_in_window():
    assert (2, 29) in window_mmdds(date(2027, 2, 28), date(2027, 3, 1))
    assert window_mmdds(date(2026, 1, 1), date(2026, 1, 1)) == set()
    assert window_mmdds(date(2020, 1, 1), date(2026, 1, 1)) is None     # > 366 days: refresh everyone


def test_age_changes_on_birthday_via_incremental_refresh():
    s = CountryStore()
    load(s, make(1, dob=date(1985, 11, 22), as_of=AS_OF))
    assert s.tables["USA"]["GLOBAL|1"].age == 40
    refresh_derived(s, AS_OF)                                  # sets watermark
    assert refresh_derived(s, date(2026, 11, 22)) == 1
    assert s.tables["USA"]["GLOBAL|1"].age == 41


def test_member_becomes_stale_on_day_91():
    s = CountryStore()
    flight = date(2026, 8, 23)                                 # 90 days before AS_OF: not stale yet
    load(s, make(1, flight=flight))
    assert s.tables["USA"]["GLOBAL|1"].stale_member is False
    refresh_derived(s, AS_OF)
    refresh_derived(s, AS_OF + timedelta(days=1))
    assert s.tables["USA"]["GLOBAL|1"].stale_member is True


def test_untouched_members_are_not_recomputed():
    s = CountryStore()
    load(s, make(1, dob=date(1985, 5, 3), flight=date(2026, 11, 1)),
         make(2, dob=date(1985, 11, 22), flight=date(2026, 11, 1), row=2))
    refresh_derived(s, AS_OF)
    assert refresh_derived(s, date(2026, 11, 22)) == 1         # only the birthday member


def test_refresh_rejects_backwards_as_of():
    s = CountryStore()
    refresh_derived(s, AS_OF)
    with pytest.raises(ValueError):
        refresh_derived(s, AS_OF - timedelta(days=1))


def test_incremental_equals_full_recompute_every_day_for_two_years():
    rng = random.Random(7)
    base = date(2024, 6, 15)
    members = []
    for i in range(1, 301):
        dob = date(rng.randint(1940, 2005), rng.randint(1, 12), rng.randint(1, 28)) if rng.random() < .9 else None
        if rng.random() < .05:
            dob = date(rng.choice([1996, 2000, 2004]), 2, 29)          # leap-day births
        flight = base - timedelta(days=rng.randint(0, 400)) if rng.random() < .9 else None
        members.append(make(i, dob=dob, flight=flight, as_of=base, row=i))
    inc = CountryStore()
    load(inc, *members)
    refresh_derived(inc, base)
    day = base
    for _ in range(730):
        day += timedelta(days=1)
        refresh_derived(inc, day)
        full = copy.deepcopy(inc)
        recompute_all(full, day)
        got = {k: (m.age, m.stale_member) for t in inc.tables.values() for k, m in t.items()}
        want = {k: (m.age, m.stale_member) for t in full.tables.values() for k, m in t.items()}
        assert got == want, f"diverged on {day}"


def test_incremental_equals_full_across_gaps_including_missed_days():
    rng = random.Random(11)
    base = date(2025, 1, 10)
    ms = [make(i, dob=date(rng.randint(1950, 2004), rng.randint(1, 12), rng.randint(1, 28)),
               flight=base - timedelta(days=rng.randint(0, 300)), as_of=base, row=i) for i in range(1, 201)]
    for gap in (1, 2, 7, 45, 120, 365, 366, 367, 800):
        s = CountryStore()
        load(s, *ms)
        refresh_derived(s, base)
        refresh_derived(s, base + timedelta(days=gap))
        full = copy.deepcopy(s)
        recompute_all(full, base + timedelta(days=gap))
        assert {k: (m.age, m.stale_member) for t in s.tables.values() for k, m in t.items()} == \
               {k: (m.age, m.stale_member) for t in full.tables.values() for k, m in t.items()}, gap


def test_sql_window_vars_match_python_window():
    from skypoints.derived import sql_window_vars
    v = sql_window_vars(date(2026, 11, 20), date(2026, 11, 22))
    assert v["BIRTHDAY_FILTER"] == "TO_CHAR(t.dob,'MMDD') IN ('1121','1122')"
    assert v["REFRESH_ALL"] == "FALSE"
    assert (v["FLIP_FROM"], v["FLIP_TO"]) == ("2026-08-22", "2026-08-23")      # flights whose day-91 is Nov 21 or Nov 22
    assert sql_window_vars(date(2020, 1, 1), date(2026, 1, 1))["REFRESH_ALL"] == "TRUE"
    assert "'0229'" in sql_window_vars(date(2027, 2, 28), date(2027, 3, 1))["BIRTHDAY_FILTER"]
