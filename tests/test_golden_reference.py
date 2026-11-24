import re
from pathlib import Path

from skypoints import config
from skypoints.golden import build, read_golden
from skypoints.reference import country_table_statements, member_all_view_sql, seed_statements

ROOT = Path(__file__).resolve().parents[1]


def test_golden_files_are_up_to_date():
    """Fails when a rule changed but the golden files were not regenerated.
    Fix: PYTHONPATH=src python -m skypoints.golden  (then re-run the Snowflake parity check)."""
    staging, rejects = build(ROOT / "sample_data")
    exp_staging, exp_rejects = read_golden(ROOT / "tests" / "golden")
    assert staging == exp_staging
    assert rejects == exp_rejects


def test_golden_pins_the_known_sample_outcomes():
    staging, rejects = read_golden(ROOT / "tests" / "golden")
    keys = {r["member_key"] for r in staging}
    assert "AUS|2" not in keys and len(staging) == 13                   # 2021-13-13 row is quarantined
    jacob = next(r for r in staging if r["member_key"] == "GLOBAL|2256")
    assert (jacob["country_code"], jacob["dob"], jacob["age"]) == ("AUS", "1985-05-03", "41")
    assert ("AUS.xlsx", "2", "BAD_DATE", "ERROR") in {tuple(r.values()) for r in rejects}


def test_seed_statements_come_from_config():
    sql = "\n".join(seed_statements())
    for code, table in config.COUNTRIES.items():
        assert f"('{code}', '{table}')" in sql
    for alias, code in config.COUNTRY_ALIASES.items():
        assert f"('{alias}', '{code}')" in sql
    assert "('GLOBAL', NULL, 'YMD_COMPACT', 'YMD_COMPACT', 'DMY_COMPACT', 'delta')" in sql
    assert "('USA', 'USA', 'MDY_COMPACT', 'MDY_COMPACT', 'MDY_COMPACT', 'delta')" in sql
    for tier in config.TIER_CODES:
        assert f"('TIER', '{tier}')" in sql


def test_seed_is_delete_then_insert_for_every_dimension():
    stmts = seed_statements()
    for table in ("DIM_COUNTRY", "DIM_COUNTRY_ALIAS", "DIM_SOURCE", "DIM_DOMAIN"):
        i = stmts.index(f"DELETE FROM {table}")
        assert stmts[i + 1].startswith(f"INSERT INTO {table} ")


def test_adding_a_country_is_one_config_edit(monkeypatch):
    monkeypatch.setitem(config.COUNTRIES, "GBR", "MEMBER_GBR")
    monkeypatch.setitem(config.COUNTRY_ALIASES, "UK", "GBR")
    assert "CREATE TABLE IF NOT EXISTS MEMBER_GBR LIKE MEMBER_IND" in country_table_statements()
    assert "SELECT 'MEMBER_GBR' AS table_name, t.* FROM MEMBER_GBR t" in member_all_view_sql()
    assert "('UK', 'GBR')" in "\n".join(seed_statements())


def test_member_all_view_covers_every_country_once():
    sql = member_all_view_sql()
    for table in config.COUNTRIES.values():
        assert len(re.findall(rf"FROM {table} t", sql)) == 1
    assert sql.count("UNION ALL") == len(config.COUNTRIES) - 1


def test_feed_mode_override_is_seeded(monkeypatch):
    monkeypatch.setitem(config.FEED_MODE, "IND", "snapshot")
    assert "('IND', 'IND', 'MDY_SLASH', 'MDY_SLASH', 'MDY_SLASH', 'snapshot')" in "\n".join(seed_statements())
    assert config.configured_snapshot_sources() == {"IND"}
