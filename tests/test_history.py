from datetime import date

from skypoints.readers import RawRow
from skypoints.stage import stage_row
from skypoints.transform import CountryStore, latest_wins

AS_OF = date(2026, 11, 21)


def member(feed, country="USA", tier="GLD", mid="100", active="A", state="CA", row=1):
    raw = RawRow("GLOBAL", "f.txt", row, feed, {
        "member_name": "Elena", "member_id": mid, "enrollment_date": "20100101",
        "last_flight_date": "20240101", "tier_code": tier, "country": country, "state": state,
        "dob": "01011980", "is_active": active})
    m, issues = stage_row(raw, AS_OF)
    assert m is not None, issues
    return m


def load(store, *ms):
    store.apply(latest_wins(ms))


def test_first_load_opens_one_current_row():
    s = CountryStore()
    load(s, member(date(2024, 1, 1)))
    chain = s.history.rows["GLOBAL|100"]
    assert len(chain) == 1 and chain[0].is_current and chain[0].valid_to is None
    assert chain[0].valid_from == date(2024, 1, 1)


def test_tier_change_closes_and_opens_version():
    s = CountryStore()
    load(s, member(date(2024, 1, 1), tier="SLV"))
    load(s, member(date(2024, 3, 1), tier="GLD"))
    a, b = s.history.rows["GLOBAL|100"]
    assert (a.tier_code, a.valid_from, a.valid_to, a.is_current) == ("SLV", date(2024, 1, 1), date(2024, 3, 1), False)
    assert (b.tier_code, b.valid_from, b.valid_to, b.is_current) == ("GLD", date(2024, 3, 1), None, True)


def test_point_in_time_question():
    s = CountryStore()
    load(s, member(date(2024, 1, 1), tier="SLV"))
    load(s, member(date(2024, 3, 1), tier="GLD"))
    assert s.history.as_of("GLOBAL|100", date(2024, 2, 15)).tier_code == "SLV"
    assert s.history.as_of("GLOBAL|100", date(2024, 3, 1)).tier_code == "GLD"   # boundary belongs to new version
    assert s.history.as_of("GLOBAL|100", date(2023, 12, 31)) is None


def test_unchanged_reload_adds_no_history():
    s = CountryStore()
    load(s, member(date(2024, 1, 1)))
    load(s, member(date(2024, 2, 1)))        # same tracked attributes, newer feed
    assert len(s.history.rows["GLOBAL|100"]) == 1


def test_reapplying_same_batch_is_idempotent():
    s = CountryStore()
    m = member(date(2024, 1, 1))
    load(s, m)
    load(s, m)
    load(s, m)
    assert len(s.history.rows["GLOBAL|100"]) == 1


def test_country_move_is_logged_from_history():
    s = CountryStore()
    load(s, member(date(2024, 1, 1), country="USA"))
    load(s, member(date(2024, 2, 1), country="CAN", state="ON"))
    load(s, member(date(2024, 5, 1), country="IND", state="TN"))
    assert s.history.moves() == [
        {"member_key": "GLOBAL|100", "from_country": "USA", "to_country": "CAN", "moved_on": date(2024, 2, 1)},
        {"member_key": "GLOBAL|100", "from_country": "CAN", "to_country": "IND", "moved_on": date(2024, 5, 1)},
    ]


def test_tier_only_change_is_not_a_move():
    s = CountryStore()
    load(s, member(date(2024, 1, 1), tier="SLV"))
    load(s, member(date(2024, 2, 1), tier="GLD"))
    assert s.history.moves() == []


def test_older_replayed_file_does_not_rewrite_history():
    s = CountryStore()
    load(s, member(date(2024, 3, 1), tier="GLD"))
    load(s, member(date(2024, 1, 1), tier="SLV"))       # late/replayed older feed: skipped by apply()
    assert len(s.history.rows["GLOBAL|100"]) == 1
    assert s.history.current("GLOBAL|100").tier_code == "GLD"


def test_exactly_one_current_row_per_member_always():
    s = CountryStore()
    for i, (tier, country) in enumerate([("SLV", "USA"), ("GLD", "USA"), ("GLD", "CAN"), ("PLT", "CAN")], start=1):
        load(s, member(date(2024, i, 1), tier=tier, country=country))
    chain = s.history.rows["GLOBAL|100"]
    assert sum(r.is_current for r in chain) == 1
    # versions tile the timeline with no gaps or overlaps
    for prev, cur in zip(chain, chain[1:]):
        assert prev.valid_to == cur.valid_from
