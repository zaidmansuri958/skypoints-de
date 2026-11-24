from datetime import date

from skypoints.identity import match_candidates, norm_name
from skypoints.readers import RawRow
from skypoints.stage import stage_row
from skypoints.transform import CountryStore, latest_wins

AS_OF = date(2026, 11, 21)


def ind(mid, name, dob="12/1/1998", enroll="1/1/2022"):
    raw = RawRow("IND", "IND.csv", int(mid), None, {
        "member_name": name, "member_id": str(mid), "enrollment_date": enroll,
        "last_flight_date": "6/15/2022", "tier_code": "SLV", "dob": dob})
    m, issues = stage_row(raw, AS_OF)
    assert m is not None, issues
    return m


def glob(mid, name, dob="01121998", enroll="20220101", country="IND"):
    raw = RawRow("GLOBAL", "g.txt", int(mid), date(2024, 1, 1), {
        "member_name": name, "member_id": str(mid), "enrollment_date": enroll,
        "last_flight_date": "20240101", "tier_code": "SLV", "country": country, "dob": dob})
    m, issues = stage_row(raw, AS_OF)
    assert m is not None, issues
    return m


def store_of(*ms):
    s = CountryStore()
    s.apply(latest_wins(ms))
    return s


def test_same_person_in_two_sources_is_proposed():
    s = store_of(ind(1, "Vikas Rao"), glob(900, "Vikas Rao"))
    pairs, notes = match_candidates(s)
    assert notes == []
    assert [(p["member_key_a"], p["member_key_b"], p["confidence"]) for p in pairs] == [
        ("GLOBAL|900", "IND|1", "HIGH")]


def test_nothing_is_merged_automatically():
    s = store_of(ind(1, "Vikas Rao"), glob(900, "Vikas Rao"))
    match_candidates(s)
    assert "IND|1" in s.tables["IND"] and "GLOBAL|900" in s.tables["IND"]     # both rows untouched


def test_name_normalisation_ignores_case_punctuation_spacing():
    assert norm_name("  Mary-Ann  O'Neil ") == norm_name("mary ann o neil") == "mary ann o neil"
    s = store_of(ind(1, "Mary-Ann O'Neil"), glob(900, "MARY ANN  O NEIL"))
    assert len(match_candidates(s)[0]) == 1


def test_different_enrollment_is_medium_confidence():
    s = store_of(ind(1, "Vikas Rao", enroll="1/1/2022"), glob(900, "Vikas Rao", enroll="20230505"))
    assert match_candidates(s)[0][0]["confidence"] == "MEDIUM"


def test_different_dob_or_missing_dob_is_not_a_candidate():
    assert match_candidates(store_of(ind(1, "Vikas Rao"), glob(900, "Vikas Rao", dob="02121998")))[0] == []
    no_dob = RawRow("GLOBAL", "g.txt", 5, date(2024, 1, 1), {
        "member_name": "Vikas Rao", "member_id": "901", "enrollment_date": "20220101",
        "last_flight_date": "20240101", "tier_code": "SLV", "country": "IND"})
    m, _ = stage_row(no_dob, AS_OF)
    assert match_candidates(store_of(ind(1, "Vikas Rao"), m))[0] == []


def test_same_source_pairs_are_not_proposed():
    s = store_of(ind(1, "Vikas Rao"), ind(2, "Vikas Rao"))
    assert match_candidates(s)[0] == []


def test_oversized_block_is_skipped_with_a_note():
    ms = [ind(i, "John Smith") for i in range(1, 12)] + [glob(900 + i, "John Smith") for i in range(1, 12)]
    pairs, notes = match_candidates(store_of(*ms), max_group=20)
    assert pairs == [] and len(notes) == 1 and "ambiguous" in notes[0]
