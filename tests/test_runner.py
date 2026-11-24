import csv
import re
import threading
from datetime import date
from pathlib import Path

import pytest

from skypoints.config import COUNTRIES
from skypoints.edge import RAW_COLUMNS, write_canonical_csv
from skypoints.golden import GOLDEN_AS_OF, SAMPLE_FILES
from skypoints.snowflake_runner import GateFailed, ReconciliationFailed, SnowflakeRunner
from skypoints.sqlrender import KNOWN_VARS, SQL_DIR, render, split_statements, statements, template_vars

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data"
GOLDEN = ROOT / "tests" / "golden"
HDR = "|H|Member_Name|Member_Id|Enrollment_Date|Last_Flight_Date|Tier_Code|Agent_Name|State|Country|DOB|Is_Active\n"
AS_OF = date(2026, 11, 22)


# ------------------------------------------------------------------ rendering and splitting

def test_render_substitutes_and_leaves_snowflake_dollar_signs_alone():
    out = render("SELECT $1, METADATA$FILENAME FROM t WHERE x = '${LOAD_ID}'", {"LOAD_ID": "L1"})
    assert out == "SELECT $1, METADATA$FILENAME FROM t WHERE x = 'L1'"


def test_render_fails_loudly_on_a_missing_variable():
    with pytest.raises(KeyError, match="TARGET_TABLE"):
        render("DELETE FROM ${TARGET_TABLE}", {"LOAD_ID": "x"})


def test_split_ignores_semicolons_in_comments_strings_and_dollar_bodies():
    sql = """
    -- a comment; with a semicolon
    SELECT 'a;b' AS x;                      /* block; comment */
    CREATE FUNCTION f() RETURNS NUMBER AS $$ SELECT 1; $$;
    SELECT 'it''s; fine';
    SELECT 1
    """
    assert split_statements(sql) == [
        "SELECT 'a;b' AS x",
        "CREATE FUNCTION f() RETURNS NUMBER AS $$ SELECT 1; $$",
        "SELECT 'it''s; fine'",
        "SELECT 1",
    ]


def test_every_sql_file_splits_and_uses_only_known_variables():
    files = sorted(p.name for p in SQL_DIR.glob("*.sql"))
    assert len(files) >= 19
    dummy = {k: "X" for k in KNOWN_VARS}
    for f in files:
        text = (SQL_DIR / f).read_text()
        unknown = template_vars(text) - KNOWN_VARS
        assert not unknown, f"{f} uses unknown template variable(s) {unknown}"
        stmts = statements(f, dummy)
        assert stmts, f
        assert not any("${" in s for s in stmts), f
        assert all(s.strip() for s in stmts), f


def test_function_file_keeps_each_body_in_one_statement():
    stmts = statements("03_functions_views.sql")
    funcs = [s for s in stmts if s.startswith("CREATE OR REPLACE FUNCTION")]
    assert [re.match(r"CREATE OR REPLACE FUNCTION (\w+)", s).group(1) for s in funcs] == [
        "CLEAN_TXT", "PARSE_SRC_DATE", "CALC_AGE", "IS_STALE"]
    assert all(s.count("$$") == 2 for s in funcs)
    assert any(s.startswith("CREATE OR REPLACE VIEW V_RAW_MEMBER_PARSED") for s in stmts)


def test_validation_file_is_all_one_insert_plus_batch_rules():
    stmts = statements("04_validate.sql", {"LOAD_ID": "L1", "AS_OF": "2026-11-21"})
    assert sum(s.startswith("INSERT INTO STG_REJECT") for s in stmts) == 3
    rules = set(re.findall(r"'([A-Z_]+)',\s*'(?:ERROR|WARNING)'", "\n".join(stmts)))
    assert {"MANDATORY", "BAD_DATE", "DATE_ORDER", "AGE_RANGE", "UNKNOWN_COUNTRY", "COUNTRY_ALIAS", "TIER_CODE",
            "ACTIVE_FLAG", "POST_CODE", "FIELD_LENGTH", "DUP_KEY", "ID_FORMAT", "ROW_SHAPE"} <= rules


# ------------------------------------------------------------------ edge converter

def test_edge_writes_one_canonical_csv_for_every_source(tmp_path):
    files = [str(SAMPLE / f) for f in SAMPLE_FILES]
    res = write_canonical_csv(files, tmp_path / "m.csv", "L1")
    rows = list(csv.reader(open(tmp_path / "m.csv", newline="", encoding="utf-8")))
    assert tuple(rows[0]) == RAW_COLUMNS
    assert len(rows) - 1 == res.rows == 14
    assert all(len(r) == len(RAW_COLUMNS) for r in rows)
    assert res.sources == {"IND", "USA", "AUS", "GLOBAL"}
    assert res.feed_dates == {"GLOBAL": date(2024, 1, 15)}
    by_file = {}
    for r in rows[1:]:
        by_file.setdefault(r[2], []).append(dict(zip(RAW_COLUMNS, r)))
    assert by_file["AUS.xlsx"][1]["enrollment_date"] == "2021-13-13"          # kept raw, not "fixed"
    assert by_file["USA.csv"][0]["enrollment_date"] == "6152022"
    assert by_file["GLOBAL_20240115.txt"][0]["feed_date"] == "2024-01-15"
    assert by_file["IND.csv"][0]["member_type"] == "I"
    assert [(s, f, i.rule_code) for s, f, i in res.header_issues] == [("GLOBAL", "GLOBAL_20240115.txt", "HEADER_SPEC")]


def test_edge_carries_pipe_shape_errors(tmp_path):
    f = tmp_path / "GLOBAL_20240101.txt"
    f.write_text(HDR + "|D|Elena|1|20101012|20121013|GLD|Sam|CA|USA|03051985\n")
    write_canonical_csv([str(f)], tmp_path / "m.csv", "L1")
    row = list(csv.DictReader(open(tmp_path / "m.csv", newline="", encoding="utf-8")))[0]
    assert row["shape_error"] == "expected 10 fields, got 9"          # header has 10 columns; the row is missing is_active


# ------------------------------------------------------------------ orchestration with a fake Snowflake

class Log:
    def __init__(self):
        self.stmts, self.lock = [], threading.Lock()

    def add(self, tag, stmt):
        with self.lock:
            self.stmts.append((tag, stmt))

    def sql(self):
        return [s for _, s in self.stmts]

    def first(self, pattern):
        for i, s in enumerate(self.sql()):
            if re.search(pattern, s, re.S):
                return i
        raise AssertionError(f"no statement matches {pattern!r}")

    def all(self, pattern):
        return [i for i, s in enumerate(self.sql()) if re.search(pattern, s, re.S)]

    def any(self, pattern):
        return bool(self.all(pattern))


class Fake:
    def __init__(self, log, answers=None, tag="main", fail_on=None):
        self.log, self.answers, self.tag, self.fail_on, self.closed = log, answers or {}, tag, fail_on, False

    def execute(self, sql):
        self.log.add(self.tag, sql)
        if self.fail_on and re.search(self.fail_on, sql, re.S):
            raise RuntimeError("boom")
        for pattern, rows in self.answers.items():
            if re.search(pattern, sql, re.S):
                return rows() if callable(rows) else rows
        return []

    def close(self):
        self.closed = True


GOOD = {
    r"SELECT DISTINCT source_file, source_row FROM STG_REJECT": [(14, 1)],
    r"SELECT COUNT\(\*\) FROM STG_MEMBER": [(13,)],
    r"SELECT COUNT\(\*\) FROM STG_APPLY": [(13,)],
    r"FROM V_REDEMPTION_ORPHANS": [(0,)],
    r"SELECT as_of FROM DERIVED_WATERMARK": [],
}


def runner(answers=None, parallel_sessions=False, fail_on=None):
    log = Log()
    merged = {**GOOD, **(answers or {})}
    main = Fake(log, merged, "main", fail_on)
    sessions = []

    def connect():
        s = Fake(log, merged, f"s{len(sessions)}", fail_on)
        sessions.append(s)
        return s
    return SnowflakeRunner(main, connect=connect if parallel_sessions else None, log=lambda m: None), log, sessions


def members():
    return [str(SAMPLE / f) for f in SAMPLE_FILES]


def test_load_runs_every_step_in_the_right_order(tmp_path):
    r, log, _ = runner()
    rep = r.load(members(), [str(SAMPLE / "redemptions_20240115.json")], GOLDEN_AS_OF,
                 max_error_rate=0.10, load_id="L1", workdir=tmp_path)
    order = [
        r"^PUT '.*members_L1\.csv'", r"^COPY INTO RAW_MEMBER_FILE", r"^PUT '.*redemptions_20240115\.json'",
        r"^COPY INTO RAW_REDEMPTION_FEED", r"^CREATE OR REPLACE TEMPORARY TABLE TMP_PARSED",
        r"^INSERT INTO STG_REJECT .*'ROW_SHAPE'", r"^INSERT INTO STG_REJECT .*'HEADER_SPEC'",
        r"SELECT DISTINCT source_file, source_row FROM STG_REJECT", r"^INSERT INTO STG_MEMBER",
        r"^INSERT INTO STG_APPLY", r"^DELETE FROM MEMBER_IND t", r"^MERGE INTO MEMBER_IND t",
        r"^MERGE INTO MEMBER_INDEX", r"^UPDATE MEMBER_HISTORY", r"^INSERT INTO MEMBER_HISTORY",
        r"^MERGE INTO REDEMPTION_FACT", r"^UPDATE REDEMPTION_FACT", r"^SELECT 'index_without_row'",
    ]
    idx = [log.first(p) for p in order]
    assert idx == sorted(idx), dict(zip(order, idx))
    assert (rep.raw_rows, rep.error_rows, rep.staged_rows, rep.applied_rows) == (14, 1, 13, 13)
    assert [s for s, st, _ in rep.steps if st == "FAILED"] == []
    assert log.any(r"^INSERT INTO PIPELINE_RUN_LOG .*'gate', 'OK'")


def test_copy_statements_carry_every_canonical_column(tmp_path):
    r, log, _ = runner()
    r.load(members(), [str(SAMPLE / "redemptions_20240115.json")], GOLDEN_AS_OF, max_error_rate=0.10,
           load_id="L1", workdir=tmp_path)
    copy = log.sql()[log.first(r"^COPY INTO RAW_MEMBER_FILE")]
    assert all(c in copy for c in RAW_COLUMNS)
    assert "$4::NUMBER" in copy and "$18" in copy and "ABORT_STATEMENT" in copy and "\\" not in copy
    red = log.sql()[log.first(r"^COPY INTO RAW_REDEMPTION_FEED")]
    assert "METADATA$FILENAME" in red and "ON_ERROR = 'CONTINUE'" in red


def test_gate_stops_the_batch_before_staging_or_merging(tmp_path):
    r, log, _ = runner({r"SELECT DISTINCT source_file, source_row FROM STG_REJECT": [(100, 40)]})
    with pytest.raises(GateFailed, match="40.0%"):
        r.load(members(), [], GOLDEN_AS_OF, max_error_rate=0.02, load_id="L1", workdir=tmp_path)
    assert not log.any(r"^INSERT INTO STG_MEMBER")
    assert not log.any(r"^MERGE INTO MEMBER_")
    assert not log.any(r"^INSERT INTO STG_APPLY")
    assert log.any(r"^INSERT INTO PIPELINE_RUN_LOG .*'gate', 'FAILED'")


def test_parallel_merge_finishes_all_deletes_before_any_upsert(tmp_path):
    r, log, sessions = runner(parallel_sessions=True)
    r.load(members(), [], GOLDEN_AS_OF, max_error_rate=0.10, load_id="L1", workdir=tmp_path)
    deletes = log.all(r"^DELETE FROM MEMBER_(IND|USA|AUS|CAN|PHL) t")
    upserts = log.all(r"^MERGE INTO MEMBER_(IND|USA|AUS|CAN|PHL) t")
    assert len(deletes) == len(upserts) == len(COUNTRIES)
    assert max(deletes) < min(upserts)                       # a moved member is never in two tables
    assert len(sessions) == 2 * len(COUNTRIES) and all(s.closed for s in sessions)
    merge_tags = {t for t, s in log.stmts if re.match(r"^MERGE INTO MEMBER_(IND|USA|AUS|CAN|PHL) t", s)}
    assert merge_tags and "main" not in merge_tags           # really ran in the extra sessions


def test_parallel_failure_surfaces_and_stops_the_load(tmp_path):
    r, log, _ = runner(parallel_sessions=True, fail_on=r"^MERGE INTO MEMBER_CAN")
    with pytest.raises(RuntimeError, match="boom"):
        r.load(members(), [], GOLDEN_AS_OF, max_error_rate=0.10, load_id="L1", workdir=tmp_path)
    assert not log.any(r"^MERGE INTO MEMBER_INDEX")           # index not advanced past a failed merge
    assert not log.any(r"^SELECT 'index_without_row'")


def test_serial_mode_is_one_transaction_and_rolls_back_on_failure(tmp_path):
    r, log, _ = runner()
    r.load(members(), [], GOLDEN_AS_OF, max_error_rate=0.10, load_id="L1", workdir=tmp_path, parallel=False)
    assert log.first("^BEGIN$") < log.first(r"^DELETE FROM MEMBER_IND t") < log.first(r"^MERGE INTO MEMBER_PHL t") \
        < log.first("^COMMIT$")
    r2, log2, _ = runner(fail_on=r"^MERGE INTO MEMBER_USA")
    with pytest.raises(RuntimeError):
        r2.load(members(), [], GOLDEN_AS_OF, max_error_rate=0.10, load_id="L2", workdir=tmp_path, parallel=False)
    assert log2.any("^ROLLBACK$") and not log2.any("^COMMIT$")


def glob_file(tmp_path):
    f = tmp_path / "GLOBAL_20240102.txt"
    f.write_text(HDR + "|D|Elena|223457|20101012|20240101|GLD|Sam|CA|USA|03051985|A\n")
    return str(f)


def test_snapshot_guard_blocks_marking_on_a_truncated_file(tmp_path):
    r, log, _ = runner({r"AS seen_cnt": [(2, 10)]})
    rep = r.load([glob_file(tmp_path)], [], GOLDEN_AS_OF, snapshot_sources={"GLOBAL"}, max_error_rate=0.5,
                 load_id="L1", workdir=tmp_path)
    assert rep.snapshot_marked == {"GLOBAL": 0}
    assert not log.any(r"^UPDATE MEMBER_\w+ t\s+SET record_status = 'MISSING_FROM_FEED'")
    assert any(s == "snapshot_GLOBAL" and st == "SKIPPED" for s, st, _ in rep.steps)


def test_snapshot_marks_per_country_when_the_file_looks_complete(tmp_path):
    r, log, _ = runner({r"AS seen_cnt": [(9, 10)], r"SELECT COUNT\(\*\) FROM MEMBER_ALL": [(1,)]})
    rep = r.load([glob_file(tmp_path)], [], GOLDEN_AS_OF, snapshot_sources={"GLOBAL"}, max_error_rate=0.5,
                 load_id="L1", workdir=tmp_path)
    marks = log.all(r"^UPDATE MEMBER_\w+ t\s+SET record_status = 'MISSING_FROM_FEED'")
    assert len(marks) == len(COUNTRIES) and rep.snapshot_marked == {"GLOBAL": 1}
    assert log.any(r"missing_since = DATE '2024-01-02'")        # the file's feed date, not today


def test_snapshot_only_applies_to_sources_present_in_the_load(tmp_path):
    r, log, _ = runner({r"AS seen_cnt": [(9, 10)]})
    r.load([str(SAMPLE / "IND.csv")], [], GOLDEN_AS_OF, snapshot_sources={"GLOBAL"}, max_error_rate=0.5,
           load_id="L1", workdir=tmp_path)
    assert not log.any(r"AS seen_cnt")


def test_derived_refresh_cases(tmp_path):
    kw = dict(max_error_rate=0.10, load_id="L1", workdir=tmp_path)
    # first run: just sets the watermark
    r, log, _ = runner()
    assert r.load(members(), [], GOLDEN_AS_OF, **kw).derived_refreshed is False
    assert log.any(r"^INSERT INTO DERIVED_WATERMARK")
    # same day: nothing to do
    r, log, _ = runner({r"SELECT as_of FROM DERIVED_WATERMARK": [(GOLDEN_AS_OF,)]})
    assert r.load(members(), [], GOLDEN_AS_OF, **kw).derived_refreshed is False
    assert not log.any(r"^UPDATE MEMBER_\w+ t\s+SET age")
    # next day: one update per country using that day's birthday window, then the watermark moves
    r, log, _ = runner({r"SELECT as_of FROM DERIVED_WATERMARK": [(GOLDEN_AS_OF,)]})
    assert r.load(members(), [], AS_OF, **kw).derived_refreshed is True
    ups = log.all(r"^UPDATE MEMBER_\w+ t\s+SET age")
    assert len(ups) == len(COUNTRIES)
    assert "TO_CHAR(t.dob,'MMDD') IN ('1122')" in log.sql()[ups[0]]
    assert log.first(r"^UPDATE MEMBER_\w+ t\s+SET age") < log.first(r"^UPDATE DERIVED_WATERMARK")
    # an as-of date before the watermark is refused
    r, _, _ = runner({r"SELECT as_of FROM DERIVED_WATERMARK": [(date(2027, 1, 1),)]})
    with pytest.raises(ValueError, match="before the derived watermark"):
        r.load(members(), [], GOLDEN_AS_OF, **kw)


def test_reconcile_violation_fails_the_load(tmp_path):
    r, _, _ = runner({r"^SELECT 'row_without_index'": [("row_without_index", "GLOBAL|1", "MEMBER_USA")]})
    with pytest.raises(ReconciliationFailed, match="row_without_index"):
        r.load(members(), [], GOLDEN_AS_OF, max_error_rate=0.10, load_id="L1", workdir=tmp_path)


def test_redemptions_only_load_skips_member_steps_but_still_reconciles(tmp_path):
    r, log, _ = runner()
    r.load([], [str(SAMPLE / "redemptions_20240115.json")], GOLDEN_AS_OF, load_id="L1", workdir=tmp_path)
    assert not log.any(r"^INSERT INTO STG_MEMBER") and not log.any(r"TMP_PARSED")
    assert log.any(r"^MERGE INTO REDEMPTION_FACT") and log.any(r"^SELECT 'index_without_row'")


def test_bad_load_id_is_rejected_before_anything_runs(tmp_path):
    r, log, _ = runner()
    with pytest.raises(ValueError):
        r.load(members(), [], GOLDEN_AS_OF, load_id="x'; DROP TABLE t; --", workdir=tmp_path)
    assert log.stmts == []


def test_deploy_runs_setup_ddl_seeds_country_tables_and_views_in_order():
    r, log, _ = runner()
    r.deploy()
    order = [r"^CREATE DATABASE", r"^CREATE TABLE IF NOT EXISTS RAW_MEMBER_FILE", r"^CREATE TABLE IF NOT EXISTS MEMBER_IND",
             r"^DELETE FROM DIM_COUNTRY$", r"^INSERT INTO DIM_SOURCE", r"^CREATE TABLE IF NOT EXISTS MEMBER_USA LIKE MEMBER_IND",
             r"^CREATE OR REPLACE VIEW MEMBER_ALL", r"^CREATE OR REPLACE FUNCTION CLEAN_TXT",
             r"^CREATE OR REPLACE VIEW V_RAW_MEMBER_PARSED", r"^CREATE OR REPLACE VIEW V_MEMBER_MATCH_CANDIDATES"]
    idx = [log.first(p) for p in order]
    assert idx == sorted(idx), dict(zip(order, idx))
    assert len(log.all(r"^CREATE TABLE IF NOT EXISTS MEMBER_\w+ LIKE MEMBER_IND")) == len(COUNTRIES) - 1


def test_parity_loads_the_golden_files_and_returns_differences():
    diff = ("staging_only_in_actual", "X.csv", "1")
    r, log, _ = runner({r"SELECT 'staging_only_in_actual'": [diff]})
    out = r.parity("L1", GOLDEN)
    assert diff in out
    assert log.first(r"^CREATE OR REPLACE TRANSIENT TABLE PARITY_STG_EXPECTED") < log.first(
        r"^INSERT INTO PARITY_STG_EXPECTED") < log.first(r"^SELECT 'staging_only_in_expected'")
    ins = "\n".join(s for s in log.sql() if s.startswith("INSERT INTO PARITY_STG_EXPECTED"))
    assert "'GLOBAL|2256'" in ins and "'AUS|2'" not in ins        # the quarantined row is not expected


# ------------------------------------------------------------------ connection setup

def _fake_connector(monkeypatch):
    import sys
    import types
    seen = {"kwargs": [], "sql": []}

    class Cur:
        def execute(self, sql): seen["sql"].append(sql)
        def fetchall(self): return []
        def close(self): pass

    class Conn:
        def cursor(self): return Cur()
        def close(self): pass

    mod = types.ModuleType("snowflake.connector")
    mod.connect = lambda **kw: seen["kwargs"].append(kw) or Conn()
    pkg = types.ModuleType("snowflake")
    pkg.connector = mod
    monkeypatch.setitem(sys.modules, "snowflake", pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", mod)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "org-acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "me")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "pw")
    monkeypatch.delenv("SNOWFLAKE_AUTHENTICATOR", raising=False)
    monkeypatch.delenv("SNOWFLAKE_ROLE", raising=False)
    return seen


def test_first_connection_does_not_ask_for_objects_that_do_not_exist_yet(monkeypatch):
    from skypoints.snowflake_runner import connect_from_env
    seen = _fake_connector(monkeypatch)
    connect_from_env()
    kw = seen["kwargs"][0]
    assert not {"database", "schema", "warehouse"} & set(kw)
    assert kw["account"] == "org-acct" and kw["user"] == "me" and kw["password"] == "pw"


def test_role_and_browser_login_are_passed_through(monkeypatch):
    from skypoints.snowflake_runner import connect_from_env
    seen = _fake_connector(monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
    connect_from_env()
    kw = seen["kwargs"][0]
    assert kw["role"] == "ACCOUNTADMIN" and kw["authenticator"] == "externalbrowser" and "password" not in kw


def test_parallel_sessions_select_the_warehouse_themselves(monkeypatch):
    from skypoints.snowflake_runner import connect_session
    seen = _fake_connector(monkeypatch)
    connect_session()
    assert seen["sql"] == ["USE WAREHOUSE SKYPOINTS_WH"]


def test_missing_credentials_give_a_clear_message(monkeypatch):
    from skypoints.snowflake_runner import connect_from_env
    _fake_connector(monkeypatch)
    monkeypatch.delenv("SNOWFLAKE_USER")
    with pytest.raises(SystemExit, match="SNOWFLAKE_USER"):
        connect_from_env()


# ------------------------------------------------------------------ the first real Snowflake run found this

class RealisticFake(Fake):
    """Like Snowflake: a non-query statement (USE, DDL) answers with a status row, not nothing."""

    def execute(self, sql):
        rows = super().execute(sql)
        if not rows and re.match(r"\s*(USE|CREATE|INSERT|MERGE|DELETE|UPDATE|ALTER|DROP)\b", sql, re.I):
            return [("Statement executed successfully.",)]
        return rows


