from datetime import date

from skypoints.readers import RawRow
from skypoints.stage import stage_row
from skypoints.transform import CountryStore, latest_wins
from skypoints.validate import duplicate_key_issues, id_length_issues

AS_OF = date(2026, 11, 21)


def member(mid="100", country="USA", feed=date(2024, 1, 15), flight="20240101", row=1, name="Elena", system="GLOBAL"):
    raw = RawRow(system, "f.txt", row, feed, {
        "member_name": name, "member_id": mid, "enrollment_date": "20100101", "last_flight_date": flight,
        "tier_code": "GLD", "country": country, "dob": "01011980", "is_active": "A",
    })
    m, issues = stage_row(raw, AS_OF)
    assert m is not None, issues
    return m


def test_latest_feed_date_wins():
    old, new = member(feed=date(2024, 1, 1), name="Old"), member(feed=date(2024, 2, 1), name="New", row=2)
    assert latest_wins([old, new])["GLOBAL|100"].member_name == "New"
    assert latest_wins([new, old])["GLOBAL|100"].member_name == "New"   # input order irrelevant


def test_tiebreak_on_last_flight_then_row():
    a = member(flight="20240101", name="A")
    b = member(flight="20240301", name="B", row=2)
    assert latest_wins([b, a])["GLOBAL|100"].member_name == "B"
    c = member(flight="20240101", name="C", row=3)
    assert latest_wins([c, a])["GLOBAL|100"].member_name == "C"          # later row wins final tie


def test_country_move_removes_from_old_table():
    store = CountryStore()
    store.apply(latest_wins([member(country="USA", feed=date(2024, 1, 1))]))
    store.apply(latest_wins([member(country="IND", feed=date(2024, 2, 1))]))
    assert "GLOBAL|100" not in store.tables["USA"]
    assert "GLOBAL|100" in store.tables["IND"]
    assert store.moved == [("GLOBAL|100", "USA", "IND")]
    assert store.index["GLOBAL|100"][0] == "IND"


def test_move_within_one_batch_keeps_only_latest_country():
    batch = [member(country="USA", feed=date(2024, 1, 1)), member(country="CAN", feed=date(2024, 3, 1), row=2)]
    store = CountryStore()
    store.apply(latest_wins(batch))
    assert sum(len(t) for t in store.tables.values()) == 1
    assert "GLOBAL|100" in store.tables["CAN"]


def test_older_replayed_file_does_not_overwrite_newer():
    store = CountryStore()
    store.apply(latest_wins([member(country="IND", feed=date(2024, 2, 1), name="New")]))
    store.apply(latest_wins([member(country="USA", feed=date(2024, 1, 1), name="Old")]))
    assert store.tables["IND"]["GLOBAL|100"].member_name == "New"
    assert "GLOBAL|100" not in store.tables["USA"]


def test_reapplying_same_batch_is_idempotent():
    batch = latest_wins([member(), member(mid="101", row=2)])
    store = CountryStore()
    store.apply(batch)
    store.apply(batch)
    assert sum(len(t) for t in store.tables.values()) == 2
    assert store.moved == []


def test_same_id_in_different_sources_do_not_collide():
    a = member(mid="1", system="GLOBAL")
    raw = RawRow("IND", "IND.csv", 1, None, {
        "member_name": "Vikas", "member_id": "1", "enrollment_date": "1/1/2022",
        "last_flight_date": "6/15/2022", "tier_code": "SLV", "dob": "12/1/1998"})
    b, _ = stage_row(raw, AS_OF)
    store = CountryStore()
    store.apply(latest_wins([a, b]))
    assert store.lookup("GLOBAL", "1").member_name == "Elena"
    assert store.lookup("IND", "1").member_name == "Vikas"


def test_duplicate_key_flagged():
    a, b = member(row=1), member(row=2)
    issues = duplicate_key_issues([a, b, member(mid="999", row=3)])
    assert set(issues) == {("f.txt", 1), ("f.txt", 2)}


def test_id_length_norm_flags_truncated_ids():
    ms = [member(mid="223457", row=1), member(mid="223458", row=2), member(mid="22345", row=3), member(mid="2256", row=4)]
    assert set(id_length_issues(ms)) == {("f.txt", 3), ("f.txt", 4)}


def test_id_length_norm_tie_goes_to_the_longer_length():
    ms = [member(mid="223457", row=1), member(mid="22345", row=2)]      # 6 vs 5, one each
    assert set(id_length_issues(ms)) == {("f.txt", 2)}
