import json
from datetime import date
from pathlib import Path

import pytest

from skypoints.pipeline import main, run
from skypoints.redemptions import RedemptionStore, flatten, join_to_members
from skypoints.transform import CountryStore, ReconciliationError

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data"
AS_OF = date(2026, 11, 21)
HDR = "|H|Member_Name|Member_Id|Enrollment_Date|Last_Flight_Date|Tier_Code|Agent_Name|State|Country|DOB|Is_Active\n"


def pipe(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text(HDR + "".join(
        f"|D|{n}|{i}|20101012|20240101|GLD|Sam|ST|{c}|03051985|A\n" for n, i, c in rows))
    return str(p)


def redemption_file(tmp_path, name, docs):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(d) for d in docs) + "\n")
    return str(p)


def doc(member, txn="T1", system=None, feed="20240115"):
    d = {"member_id": member, "feed_date": feed, "redemptions": [
        {"txn_id": txn, "txn_date": "20240110", "partner": "AeroLink", "miles_redeemed": 1000, "status": "COMPLETED"}]}
    if system:
        d["source_system"] = system
    return d


# ------------------------------------------------------------------ reconcile

def clean_store(tmp_path):
    res = run([pipe(tmp_path, "GLOBAL_20240101.txt", [("Elena", "100", "USA"), ("Ravi", "101", "IND")])], [], AS_OF)
    return res.store


def test_consistent_store_reconciles(tmp_path):
    assert clean_store(tmp_path).reconcile() == []


def test_missing_index_entry_is_detected(tmp_path):
    s = clean_store(tmp_path)
    s.index.pop("GLOBAL|100")
    assert any("missing from the index" in p for p in s.reconcile())


def test_index_pointing_to_wrong_country_is_detected(tmp_path):
    s = clean_store(tmp_path)
    s.index["GLOBAL|100"] = ("IND", None)
    assert any("index says IND" in p or "index points to IND" in p for p in s.reconcile())


def test_member_in_two_country_tables_is_detected(tmp_path):
    s = clean_store(tmp_path)
    s.tables["CAN"]["GLOBAL|100"] = s.tables["USA"]["GLOBAL|100"]
    assert any("several country tables" in p for p in s.reconcile())


def test_row_in_table_of_another_country_is_detected(tmp_path):
    s = clean_store(tmp_path)
    s.tables["IND"]["GLOBAL|100"] = s.tables["USA"].pop("GLOBAL|100")     # country_code still USA
    assert any("stored in MEMBER_IND but country_code is USA" in p for p in s.reconcile())


def test_history_disagreeing_with_index_is_detected(tmp_path):
    s = clean_store(tmp_path)
    s.history.current("GLOBAL|100").country_code = "CAN"
    assert any("history says CAN" in p for p in s.reconcile())


def test_run_raises_instead_of_publishing_an_inconsistent_store(tmp_path):
    s = clean_store(tmp_path)
    s.index.pop("GLOBAL|100")
    with pytest.raises(ReconciliationError):
        run([pipe(tmp_path, "GLOBAL_20240102.txt", [("Mateo", "102", "CAN")])], [], AS_OF, store=s)


def test_normal_country_move_stays_consistent(tmp_path):
    s = clean_store(tmp_path)
    run([pipe(tmp_path, "GLOBAL_20240102.txt", [("Elena", "100", "CAN")])], [], AS_OF, store=s)
    assert s.reconcile() == [] and "GLOBAL|100" in s.tables["CAN"]


# ------------------------------------------------------------------ gate

def test_gate_stops_the_load_before_anything_is_written():
    res = run([str(SAMPLE / "AUS.xlsx")], [str(SAMPLE / "redemptions_20240115.json")], AS_OF, max_error_rate=0.02)
    assert res.gate_failed
    assert sum(len(t) for t in res.store.tables.values()) == 0         # no good rows loaded either
    assert res.redemptions.facts == {}
    assert any(r["rule_code"] == "BAD_DATE" for r in res.rejects)       # but the rejects are still reported


def test_gate_passes_under_threshold_and_loads():
    res = run([str(SAMPLE / "AUS.xlsx")], [], AS_OF, max_error_rate=0.5)
    assert not res.gate_failed and len(res.store.tables["AUS"]) == 2


def test_no_gate_when_threshold_not_given():
    assert not run([str(SAMPLE / "AUS.xlsx")], [], AS_OF).gate_failed


def test_cli_exit_codes(tmp_path):
    base = ["--members", str(SAMPLE / "AUS.xlsx"), "--out", str(tmp_path), "--as-of", "2026-11-21"]
    assert main(base) == 2
    assert main(base + ["--max-error-rate", "0.5"]) == 0


# ------------------------------------------------------------------ redemptions

def test_orphan_is_linked_when_its_member_arrives_later(tmp_path):
    reds = RedemptionStore()
    r1 = run([], [redemption_file(tmp_path, "r_20240115.json", [doc("100")])], AS_OF, redemption_store=reds)
    assert len(r1.redemptions.orphans()) == 1 and r1.orphans_linked == 0
    r2 = run([pipe(tmp_path, "GLOBAL_20240116.txt", [("Elena", "100", "USA")])], [], AS_OF,
             redemption_store=reds)
    assert r2.orphans_linked == 1 and reds.orphans() == []
    assert reds.facts["T1"].member_key == "GLOBAL|100"


def test_resolve_is_idempotent(tmp_path):
    res = run([pipe(tmp_path, "GLOBAL_20240101.txt", [("Elena", "100", "USA")])],
              [redemption_file(tmp_path, "r.json", [doc("100")])], AS_OF)
    assert res.orphans_linked == 1
    assert res.redemptions.resolve(res.store) == 0


def test_redemption_country_follows_the_member_when_she_moves(tmp_path):
    reds, store = RedemptionStore(), CountryStore()
    run([pipe(tmp_path, "GLOBAL_20240101.txt", [("Elena", "100", "USA")])],
        [redemption_file(tmp_path, "r.json", [doc("100")])], AS_OF, store=store, redemption_store=reds)
    assert join_to_members(reds, store)[0]["country_code"] == "USA"
    run([pipe(tmp_path, "GLOBAL_20240102.txt", [("Elena", "100", "CAN")])], [], AS_OF, store=store,
        redemption_store=reds)
    row = join_to_members(reds, store)[0]
    assert row["country_code"] == "CAN" and row["member_key"] == "GLOBAL|100"   # no stale country stored


def test_feed_can_reference_a_per_country_id_space(tmp_path):
    res = run([str(SAMPLE / "IND.csv"), str(SAMPLE / "USA.csv")],
              [redemption_file(tmp_path, "r.json", [doc("1", "A", system="IND"), doc("1", "B", system="USA")])], AS_OF)
    rows = {r["txn_id"]: r for r in join_to_members(res.redemptions, res.store)}
    assert (rows["A"]["member_name"], rows["A"]["country_code"]) == ("Vikas", "IND")
    assert (rows["B"]["member_name"], rows["B"]["country_code"]) == ("Sam", "USA")


def test_unknown_source_system_is_rejected(tmp_path):
    out = list(flatten(redemption_file(tmp_path, "r.json", [doc("1", system="MARS")])))
    assert out[0][0] is None and out[0][1][0].rule_code == "UNKNOWN_SOURCE"


# ------------------------------------------------------------------ pipeline wiring for fixes 1, 2, 3, 4

def test_snapshot_run_soft_deletes_a_departed_member(tmp_path):
    store = CountryStore()
    run([pipe(tmp_path, "GLOBAL_20240101.txt", [("A", "1", "USA"), ("B", "2", "USA"), ("C", "3", "USA"),
                                                  ("D", "4", "USA"), ("E", "5", "USA")])], [], AS_OF, store=store,
        snapshot_sources=("GLOBAL",))
    res = run([pipe(tmp_path, "GLOBAL_20240102.txt", [("A", "1", "USA"), ("B", "2", "USA"), ("C", "3", "USA"),
                                                        ("D", "4", "USA")])], [], AS_OF, store=store,
              snapshot_sources=("GLOBAL",))
    assert res.snapshot_marked == {"GLOBAL": 1}
    gone = store.tables["USA"]["GLOBAL|5"]
    assert gone.record_status == "MISSING_FROM_FEED" and gone.missing_since == date(2024, 1, 2)
    assert store.reconcile() == []


def test_delta_mode_never_deletes(tmp_path):
    store = CountryStore()
    run([pipe(tmp_path, "GLOBAL_20240101.txt", [("A", "1", "USA"), ("B", "2", "USA")])], [], AS_OF, store=store)
    run([pipe(tmp_path, "GLOBAL_20240102.txt", [("A", "1", "USA")])], [], AS_OF, store=store)
    assert all(m.record_status == "ACTIVE" for m in store.tables["USA"].values())


def test_truncated_snapshot_is_blocked_and_reported(tmp_path):
    store = CountryStore()
    run([pipe(tmp_path, "GLOBAL_20240101.txt", [(f"M{i}", str(i), "USA") for i in range(1, 11)])], [], AS_OF,
        store=store, snapshot_sources=("GLOBAL",))
    res = run([pipe(tmp_path, "GLOBAL_20240102.txt", [("M1", "1", "USA")])], [], AS_OF, store=store,
              snapshot_sources=("GLOBAL",))
    assert res.snapshot_marked == {"GLOBAL": 0}
    assert any("partial file" in n for n in res.notes)
    assert any(r["rule_code"] == "SNAPSHOT_GUARD" for r in res.rejects)
    assert all(m.record_status == "ACTIVE" for m in store.tables["USA"].values())


def test_invalid_row_still_counts_as_seen_so_member_is_not_marked_missing(tmp_path):
    store = CountryStore()
    run([pipe(tmp_path, "GLOBAL_20240101.txt", [("A", "1", "USA"), ("B", "2", "USA")])], [], AS_OF, store=store,
        snapshot_sources=("GLOBAL",))
    bad = tmp_path / "GLOBAL_20240102.txt"
    bad.write_text(HDR + "|D|A|1|20101012|20240101|GLD|Sam|ST|USA|03051985|A\n"
                         "|D|B|2|99999999|20240101|GLD|Sam|ST|USA|03051985|A\n")     # B's row is rejected
    run([str(bad)], [], AS_OF, store=store, snapshot_sources=("GLOBAL",))
    assert store.tables["USA"]["GLOBAL|2"].record_status == "ACTIVE"


def test_unknown_snapshot_source_is_an_error():
    with pytest.raises(ValueError):
        run([], [], AS_OF, snapshot_sources=("NOPE",))


def test_pipeline_refreshes_derived_columns_on_the_next_day(tmp_path):
    store = CountryStore()
    f1 = pipe(tmp_path, "GLOBAL_20240101.txt", [("A", "1", "USA")])
    run([f1], [], date(2026, 5, 2), store=store)
    assert store.tables["USA"]["GLOBAL|1"].age == 40                  # born 1985-05-03
    res = run([], [], date(2026, 5, 3), store=store)
    assert res.derived_refreshed == 1 and store.tables["USA"]["GLOBAL|1"].age == 41


def test_pipeline_reports_history_and_candidates(tmp_path):
    store = CountryStore()
    run([pipe(tmp_path, "GLOBAL_20240101.txt", [("Elena", "100", "USA")])], [], AS_OF, store=store)
    run([pipe(tmp_path, "GLOBAL_20240102.txt", [("Elena", "100", "CAN")])], [], AS_OF, store=store)
    assert [m["to_country"] for m in store.history.moves()] == ["CAN"]
    res = run([str(SAMPLE / "IND.csv"), pipe(tmp_path, "GLOBAL_20240103.txt", [("Vikas", "900", "IND")])],
              [], AS_OF)
    # IND|1 Vikas dob 1998-12-01 vs GLOBAL dob 1985-05-03: different people, so no candidate
    assert res.match_candidates == []


# ------------------------------------------------------------------ the live-demo story (day 1 -> day 2)

def test_demo_day_two_moves_elena_and_upgrades_her_tier():
    from skypoints.history import HistoryStore  # noqa: F401  (documenting what is asserted below)
    store, reds = CountryStore(), RedemptionStore()
    run([str(SAMPLE / "GLOBAL_20240115.txt")], [str(SAMPLE / "redemptions_20240115.json")], AS_OF,
        store=store, redemption_store=reds)
    assert "GLOBAL|223457" in store.tables["USA"]
    assert join_to_members(reds, store)[0]["country_code"] == "USA"

    res = run([str(SAMPLE / "GLOBAL_20240116.txt")], [], AS_OF, store=store, redemption_store=reds)
    assert not res.gate_failed and store.reconcile() == []
    assert "GLOBAL|223457" not in store.tables["USA"] and "GLOBAL|223457" in store.tables["CAN"]
    # the history answers "what was she, and when did she move?"
    assert store.history.as_of("GLOBAL|223457", date(2024, 1, 15)).tier_code == "GLD"
    assert store.history.as_of("GLOBAL|223457", date(2024, 1, 16)).tier_code == "PLT"
    assert store.history.moves() == [{"member_key": "GLOBAL|223457", "from_country": "USA",
                                      "to_country": "CAN", "moved_on": date(2024, 1, 16)}]
    # her earlier redemptions now report her CURRENT country, with nothing re-loaded
    assert {r["country_code"] for r in join_to_members(reds, store)} == {"CAN"}
    # Ravi was unchanged: no new history version
    assert len(store.history.rows["GLOBAL|223458"]) == 1
