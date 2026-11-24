import json
from datetime import date
from pathlib import Path

from skypoints.pipeline import run, write_outputs
from skypoints.redemptions import RedemptionStore, flatten

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data"
AS_OF = date(2026, 11, 21)


def write_ndjson(tmp_path, docs, name="redemptions_20240115.json"):
    p = tmp_path / name
    p.write_text("\n".join(d if isinstance(d, str) else json.dumps(d) for d in docs) + "\n")
    return p


def doc(member="223457", feed="20240115", txns=None):
    return {"member_id": member, "feed_date": feed, "redemptions": txns if txns is not None else [
        {"txn_id": "RX1", "txn_date": "20240110", "partner": "AeroLink", "miles_redeemed": 12000, "status": "COMPLETED"},
        {"txn_id": "RX2", "txn_date": "20240113", "partner": "SkyPoints", "miles_redeemed": 5000, "status": "PENDING"},
    ]}


def test_flatten_one_row_per_transaction(tmp_path):
    rows = list(flatten(write_ndjson(tmp_path, [doc()])))
    assert [r.txn_id for r, _, _ in rows] == ["RX1", "RX2"]
    r = rows[0][0]
    assert (r.member_id, r.txn_date, r.miles_redeemed, r.status) == ("223457", date(2024, 1, 10), 12000, "COMPLETED")
    assert r.feed_date == date(2024, 1, 15)


def test_flatten_rejects_bad_transactions(tmp_path):
    bad = doc(txns=[
        {"txn_id": "", "txn_date": "20240110", "miles_redeemed": 10, "status": "COMPLETED"},
        {"txn_id": "A", "txn_date": "20241340", "miles_redeemed": 10, "status": "COMPLETED"},
        {"txn_id": "B", "txn_date": "20240110", "miles_redeemed": -5, "status": "COMPLETED"},
        {"txn_id": "C", "txn_date": "20240110", "miles_redeemed": "lots", "status": "COMPLETED"},
        {"txn_id": "D", "txn_date": "20240110", "miles_redeemed": 10, "status": "WEIRD"},   # warning only
    ])
    out = list(flatten(write_ndjson(tmp_path, [bad])))
    kept = [r for r, _, _ in out if r]
    assert [r.txn_id for r in kept] == ["D"]
    assert {i.rule_code for _, iss, _ in out for i in iss} >= {"MANDATORY", "BAD_DATE", "MILES", "STATUS"}


def test_flatten_survives_bad_json_line(tmp_path):
    out = list(flatten(write_ndjson(tmp_path, ["{not json", doc()])))
    assert out[0][0] is None and out[0][1][0].rule_code == "JSON_PARSE"
    assert len([r for r, _, _ in out if r]) == 2


def test_missing_redemptions_array(tmp_path):
    out = list(flatten(write_ndjson(tmp_path, [{"member_id": "1", "feed_date": "20240115"}])))
    assert out[0][0] is None and out[0][1][0].rule_code == "JSON_SHAPE"


def test_status_moves_pending_to_completed_latest_feed_wins(tmp_path):
    store = RedemptionStore()
    for r, _, _ in flatten(write_ndjson(tmp_path, [doc(feed="20240115")], "a_20240115.json")):
        store.merge(r)
    later = doc(feed="20240120", txns=[
        {"txn_id": "RX2", "txn_date": "20240113", "partner": "SkyPoints", "miles_redeemed": 5000, "status": "COMPLETED"}])
    for r, _, _ in flatten(write_ndjson(tmp_path, [later], "b_20240120.json")):
        store.merge(r)
    assert store.facts["RX2"].status == "COMPLETED"
    # an older replay must not revert it
    older = doc(feed="20240101", txns=[
        {"txn_id": "RX2", "txn_date": "20240101", "partner": "SkyPoints", "miles_redeemed": 5000, "status": "PENDING"}])
    for r, _, _ in flatten(write_ndjson(tmp_path, [older], "c_20240101.json")):
        store.merge(r)
    assert store.facts["RX2"].status == "COMPLETED"
    assert len(store.facts) == 2


def files(*names):
    return [str(SAMPLE / n) for n in names]


def test_end_to_end_sample_data():
    res = run(files("IND.csv", "USA.csv", "AUS.xlsx", "GLOBAL_20240115.txt"),
              files("redemptions_20240115.json"), AS_OF)
    t = res.store.tables
    # per-country routing
    assert len(t["IND"]) == 4 and len(t["USA"]) == 4 and len(t["AUS"]) == 3
    assert len(t["CAN"]) == 1 and len(t["PHL"]) == 1
    # the AUS row with enrollment 2021-13-13 is quarantined, not loaded
    assert "AUS|2" not in t["AUS"]
    # 14 member rows (3 IND + 3 USA + 3 AUS + 5 GLOBAL) + 2 redemption transactions
    assert res.total_rows == 16 and res.error_rows == 1
    rules = {(r["rule_code"], r["severity"]) for r in res.rejects}
    assert ("BAD_DATE", "ERROR") in rules
    assert {("COUNTRY_ALIAS", "WARNING"), ("ID_FORMAT", "WARNING"), ("HEADER_SPEC", "WARNING")} <= rules
    # same id "1" lives in three sources without colliding
    assert {t["IND"]["IND|1"].member_name, t["USA"]["USA|1"].member_name, t["AUS"]["AUS|1"].member_name} == {"Vikas", "Sam", "Mike"}
    # derived columns
    assert t["IND"]["IND|3"].age == 74 and t["IND"]["IND|3"].member_type == "I"
    assert t["IND"]["IND|2"].member_type == "C"


def test_end_to_end_redemption_join_to_member():
    res = run(files("GLOBAL_20240115.txt"), files("redemptions_20240115.json"), AS_OF)
    from skypoints.redemptions import join_to_members
    rows = {r["txn_id"]: r for r in join_to_members(res.redemptions, res.store)}
    assert rows["RX10091"]["member_name"] == "Elena" and rows["RX10091"]["country_code"] == "USA"
    assert rows["RX10092"]["status"] == "PENDING"


def test_orphan_redemption_has_no_member(tmp_path):
    p = write_ndjson(tmp_path, [doc(member="999999")])
    res = run(files("GLOBAL_20240115.txt"), [str(p)], AS_OF)
    from skypoints.redemptions import join_to_members
    assert all(r["member_key"] is None for r in join_to_members(res.redemptions, res.store))


def test_write_outputs(tmp_path):
    res = run(files("IND.csv", "GLOBAL_20240115.txt"), files("redemptions_20240115.json"), AS_OF)
    write_outputs(res, tmp_path)
    assert (tmp_path / "MEMBER_IND.csv").exists() and (tmp_path / "STG_REJECT.csv").exists()
    assert (tmp_path / "REDEMPTION_WITH_MEMBER.csv").exists()


def test_move_across_two_daily_files(tmp_path):
    day1 = tmp_path / "GLOBAL_20240101.txt"
    day2 = tmp_path / "GLOBAL_20240102.txt"
    hdr = "|H|Member_Name|Member_Id|Enrollment_Date|Last_Flight_Date|Tier_Code|Agent_Name|State|Country|DOB|Is_Active\n"
    day1.write_text(hdr + "|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03051985|A\n")
    day2.write_text(hdr + "|D|Elena|223457|20101012|20121013|GLD|Sam|ON|CAN|03051985|A\n")
    res = run([str(day1)], [], AS_OF)
    res = run([str(day2)], [], AS_OF, store=res.store)
    assert "GLOBAL|223457" in res.store.tables["CAN"]
    assert "GLOBAL|223457" not in res.store.tables["USA"]
    assert len(res.store.moved) == 1


def test_gate_exit_code(tmp_path):
    from skypoints.pipeline import main
    args = ["--members", *files("AUS.xlsx"), "--out", str(tmp_path), "--as-of", "2026-11-21"]
    assert main(args) == 2                                   # 1 of 3 rows bad -> exceeds 2%
    assert main(args + ["--max-error-rate", "0.5"]) == 0
