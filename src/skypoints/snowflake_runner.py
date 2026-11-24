"""Snowflake runner: ingestion + orchestration for the SQL pipeline.

    python -m skypoints.snowflake_runner deploy
    python -m skypoints.snowflake_runner load --members ... --redemptions ... [--as-of ...]
    python -m skypoints.snowflake_runner demo          # deploy + load sample data + parity check
    python -m skypoints.snowflake_runner parity --load-id L2026...

Credentials come from environment variables, never arguments (see docs/SNOWFLAKE_RUNBOOK.md).

All orchestration goes through the small ``Executor`` interface (execute one SQL string, get
rows back), so the step order, gate, parallel merge and failure behaviour are unit-tested
offline with a fake executor. What no offline test can prove is the SQL itself running on
Snowflake: that is what `demo` + the parity diff exist to show.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .config import COUNTRIES, SNAPSHOT_DROP_GUARD, STALE_DAYS, configured_snapshot_sources
from .derived import sql_window_vars
from .edge import RAW_COLUMNS, EdgeResult, write_canonical_csv
from .golden import GOLDEN_AS_OF, SAMPLE_FILES, read_golden
from .reference import country_table_statements, member_all_view_sql, seed_statements
from .sqlrender import statements

_LOAD_ID = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class Executor(Protocol):
    def execute(self, sql: str) -> list[tuple]: ...
    def close(self) -> None: ...


class GateFailed(RuntimeError):
    """Error rate over the threshold: the batch was stopped before staging."""


class ReconciliationFailed(RuntimeError):
    """The index, country tables and history disagree after a load."""


@dataclass
class LoadReport:
    load_id: str
    raw_rows: int = 0
    error_rows: int = 0
    staged_rows: int = 0
    applied_rows: int = 0
    snapshot_marked: dict[str, int] = field(default_factory=dict)
    derived_refreshed: bool = False
    steps: list[tuple[str, str, str]] = field(default_factory=list)       # (step, status, detail)


def _q(value: object) -> str:
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def new_load_id() -> str:
    return "L" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


class SnowflakeRunner:
    def __init__(self, main: Executor, connect: Callable[[], Executor] | None = None,
                 sql_dir: Path | None = None, log: Callable[[str], None] = print):
        """``main`` is the session used for everything order- or session-dependent (temp tables,
        transactions). ``connect`` opens extra sessions so per-country merges can run in parallel."""
        self.main, self.connect, self.sql_dir, self._log = main, connect, sql_dir, log
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    def run_file(self, name: str, variables: dict[str, str] | None = None,
                 ex: Executor | None = None) -> list[tuple]:
        """Run every statement in a SQL file; returns the rows of the last statement."""
        ex, rows = ex or self.main, []
        for stmt in statements(name, variables, self.sql_dir):
            rows = ex.execute(stmt)
        return rows

    def query_file(self, name: str, variables: dict[str, str]) -> list[list[tuple]]:
        """Run a file of check queries; returns the rows of each SELECT / WITH statement.

        Other statements (the leading USE SCHEMA) are executed but their reply is NOT a result:
        Snowflake answers them with a row saying 'Statement executed successfully.', which an
        earlier version counted as a violation."""
        out = []
        for stmt in statements(name, variables, self.sql_dir):
            out.append(self.main.execute(stmt))
        return out

    def _step(self, report: LoadReport, step: str, status: str, detail: str = "") -> None:
        report.steps.append((step, status, detail))
        self._log(f"[{report.load_id}] {step}: {status} {detail}".rstrip())
        try:
            self.main.execute(
                "INSERT INTO PIPELINE_RUN_LOG (load_id, step, status, detail) VALUES "
                f"({_q(report.load_id)}, {_q(step)}, {_q(status)}, {_q(detail[:1900])})")
        except Exception:        # logging must never mask the real failure
            pass

    # ------------------------------------------------------------------ deploy
    def deploy(self) -> None:
        """Create/refresh every object. Idempotent; safe to re-run."""
        for f in ("00_setup.sql", "01_ddl_raw_staging.sql", "02_ddl_targets.sql"):
            self.run_file(f)
        for stmt in seed_statements():                     # reference data comes from config.py
            self.main.execute(stmt)
        for stmt in country_table_statements():            # one table per configured country
            self.main.execute(stmt)
        self.main.execute(member_all_view_sql())           # generated from the same registry
        self.run_file("03_functions_views.sql")
        self.run_file("11_identity.sql")
        self._log("deploy complete")

    # ------------------------------------------------------------------ ingestion
    def _put_and_copy_members(self, edge_csv: Path, load_id: str) -> None:
        self.main.execute(f"PUT 'file://{edge_csv.as_posix()}' @LANDING/{load_id}/members/ "
                          "AUTO_COMPRESS=TRUE OVERWRITE=TRUE")
        cols = ", ".join(RAW_COLUMNS)
        sel = ", ".join(f"${i}::NUMBER" if c == "source_row" else f"${i}"
                        for i, c in enumerate(RAW_COLUMNS, start=1))
        self.main.execute(f"COPY INTO RAW_MEMBER_FILE ({cols}) FROM (SELECT {sel} FROM "
                          f"@LANDING/{load_id}/members/) FILE_FORMAT = (FORMAT_NAME = 'FF_CANON_CSV') "
                          "ON_ERROR = 'ABORT_STATEMENT'")

    def _put_and_copy_redemptions(self, files: list[str], load_id: str) -> int:
        for f in files:
            self.main.execute(f"PUT 'file://{Path(f).resolve().as_posix()}' "
                              f"@LANDING/{load_id}/redemptions/ AUTO_COMPRESS=TRUE OVERWRITE=TRUE")
        res = self.main.execute(
            f"COPY INTO RAW_REDEMPTION_FEED (load_id, source_file, doc) FROM (SELECT '{load_id}', "
            f"METADATA$FILENAME, $1 FROM @LANDING/{load_id}/redemptions/) "
            "FILE_FORMAT = (FORMAT_NAME = 'FF_JSON') ON_ERROR = 'CONTINUE'")
        # COPY returns (file, status, rows_parsed, rows_loaded, error_limit, errors_seen, ...)
        return sum(int(r[5]) for r in res if len(r) > 5 and str(r[5]).isdigit())

    # ------------------------------------------------------------------ the load
    def load(self, member_files: list[str], redemption_files: list[str], as_of: date,
             snapshot_sources: set[str] | None = None, max_error_rate: float = 0.02,
             parallel: bool = True, load_id: str | None = None, workdir: Path | None = None,
             snapshot_guard: float = SNAPSHOT_DROP_GUARD) -> LoadReport:
        load_id = load_id or new_load_id()
        if not _LOAD_ID.match(load_id):
            raise ValueError("load_id must be 1-40 letters, digits or underscores")
        snapshot_sources = {s.upper() for s in (snapshot_sources or set())} | configured_snapshot_sources()
        report = LoadReport(load_id)
        base = {"LOAD_ID": load_id, "AS_OF": as_of.isoformat(), "STALE_DAYS": str(STALE_DAYS)}
        edge = EdgeResult()
        workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="skypoints_"))

        # 1. edge convert + land + COPY into RAW
        if member_files:
            edge = write_canonical_csv(member_files, workdir / f"members_{load_id}.csv", load_id)
            self._put_and_copy_members(workdir / f"members_{load_id}.csv", load_id)
            report.raw_rows = edge.rows
            self._step(report, "ingest_members", "OK", f"{edge.rows} rows from {len(edge.files)} file(s)")
        if redemption_files:
            bad = self._put_and_copy_redemptions(redemption_files, load_id)
            self._step(report, "ingest_redemptions", "OK",
                       f"{len(redemption_files)} file(s)" + (f", {bad} unparseable line(s) skipped" if bad else ""))

        if member_files:
            # 2. validate (same session: TMP_PARSED is session-scoped) + file-level header issues
            self.run_file("03b_parse_load.sql", base)
            self.run_file("04_validate.sql", base)
            for system, fname, issue in edge.header_issues:
                self.main.execute(
                    "INSERT INTO STG_REJECT (load_id, source_system, source_file, source_row, member_key, "
                    "rule_code, severity, column_name, bad_value, reason) VALUES "
                    f"({_q(load_id)}, {_q(system)}, {_q(fname)}, 0, NULL, {_q(issue.rule_code)}, "
                    f"{_q(issue.severity)}, NULL, {_q(issue.bad_value)}, {_q(issue.reason)})")

            # 3. GATE, before anything is staged or merged
            total, errors = self.main.execute(
                f"SELECT (SELECT COUNT(*) FROM RAW_MEMBER_FILE WHERE load_id = '{load_id}'), "
                f"(SELECT COUNT(*) FROM (SELECT DISTINCT source_file, source_row FROM STG_REJECT "
                f"WHERE load_id = '{load_id}' AND severity = 'ERROR' AND source_system <> 'REDEMPTION'))")[0]
            report.raw_rows, report.error_rows = int(total), int(errors)
            rate = report.error_rows / report.raw_rows if report.raw_rows else 0.0
            if rate > max_error_rate:
                self._step(report, "gate", "FAILED",
                           f"error rate {rate:.1%} > {max_error_rate:.1%}; nothing was staged or merged")
                raise GateFailed(f"error rate {rate:.1%} exceeds {max_error_rate:.1%} "
                                 f"({report.error_rows}/{report.raw_rows} rows); see STG_REJECT for load {load_id}")
            self._step(report, "gate", "OK", f"error rate {rate:.1%}")

            # 4. stage -> winners -> per-country split
            self.run_file("05_stage_member.sql", base)
            report.staged_rows = int(self.main.execute(
                f"SELECT COUNT(*) FROM STG_MEMBER WHERE load_id = '{load_id}'")[0][0])
            self._step(report, "stage", "OK", f"{report.staged_rows} rows staged")
            self.run_file("06_prepare_apply.sql", base)
            report.applied_rows = int(self.main.execute(
                f"SELECT COUNT(*) FROM STG_APPLY WHERE load_id = '{load_id}'")[0][0])
            self._merge_countries(base, parallel)
            self._step(report, "merge_countries", "OK",
                       f"{report.applied_rows} winners, {'parallel' if parallel and self.connect else 'serial'}")

            # 5. routing index + SCD2 history
            self.run_file("08_history_index.sql", base)
            self._step(report, "index_history", "OK")

            # 6. full-snapshot sources: soft-delete members the extract no longer lists
            for system in sorted(snapshot_sources & edge.sources):
                report.snapshot_marked[system] = self._mark_missing(
                    report, base, system, edge.feed_dates.get(system, as_of), snapshot_guard)

        # 7. redemptions (also re-links older orphans whose member arrived in this load)
        if redemption_files:
            self.run_file("10_redemptions.sql", base)
            orphans = int(self.main.execute("SELECT COUNT(*) FROM V_REDEMPTION_ORPHANS")[0][0])
            self._step(report, "redemptions", "OK", f"{orphans} orphan(s) remain")

        # 8. keep age / stale_member current by touching only rows that changed
        report.derived_refreshed = self._refresh_derived(report, base, as_of)

        # 9. reconcile: index, tables and history must agree before the load counts as done
        violations = [r for rows in self.query_file("12_reconcile.sql", base) for r in rows]
        if violations:
            self._step(report, "reconcile", "FAILED", f"{len(violations)} violation(s), first: {violations[0]}")
            raise ReconciliationFailed(f"{len(violations)} inconsistencies, first: {violations[0]}")
        self._step(report, "reconcile", "OK")
        return report

    # ------------------------------------------------------------------ steps
    def _country_vars(self, base: dict[str, str]) -> list[dict[str, str]]:
        return [{**base, "COUNTRY_CODE": c, "TARGET_TABLE": t} for c, t in sorted(COUNTRIES.items())]

    def _merge_countries(self, base: dict[str, str], parallel: bool) -> None:
        """Phase 1 deletes moved members from their OLD country, phase 2 upserts into the NEW one.
        Every phase-1 statement finishes before any phase-2 statement starts, so a moved member
        is never in two country tables at once. Parallel sessions are used for speed; serial mode
        wraps both phases in one transaction for readers who need strict atomicity."""
        jobs = self._country_vars(base)
        if parallel and self.connect:
            for phase in ("07a_move_out.sql", "07b_upsert.sql"):
                with ThreadPoolExecutor(max_workers=min(len(jobs), 4)) as pool:
                    futures = [pool.submit(self._run_in_new_session, phase, v) for v in jobs]
                    for f in futures:
                        f.result()                      # re-raise the first failure
        else:
            self.main.execute("BEGIN")
            try:
                for phase in ("07a_move_out.sql", "07b_upsert.sql"):
                    for v in jobs:
                        self.run_file(phase, v)
                self.main.execute("COMMIT")
            except Exception:
                self.main.execute("ROLLBACK")
                raise

    def _run_in_new_session(self, name: str, variables: dict[str, str]) -> None:
        ex = self.connect()
        try:
            self.run_file(name, variables, ex=ex)
        finally:
            ex.close()

    def _mark_missing(self, report: LoadReport, base: dict[str, str], system: str,
                      feed_date: date, guard: float) -> int:
        v = {**base, "SOURCE_SYSTEM": system, "FEED_DATE": feed_date.isoformat()}
        seen, active = (int(x) for x in self.run_file("09a_snapshot_guard.sql", v)[0])
        if seen == 0 or (active and seen < (1 - guard) * active):
            self._step(report, f"snapshot_{system}", "SKIPPED",
                       f"snapshot lists {seen} but {active} are active; treated as a partial file, nobody marked missing")
            return 0
        for cv in self._country_vars(v):
            self.run_file("09_snapshot_missing.sql", cv)
        after = int(self.main.execute(
            f"SELECT COUNT(*) FROM MEMBER_ALL WHERE source_system = '{system}' "
            f"AND record_status = 'MISSING_FROM_FEED' AND missing_since = DATE '{feed_date.isoformat()}'")[0][0])
        self._step(report, f"snapshot_{system}", "OK", f"{after} member(s) soft-deleted as of {feed_date}")
        return after

    def _refresh_derived(self, report: LoadReport, base: dict[str, str], as_of: date) -> bool:
        rows = self.main.execute("SELECT as_of FROM DERIVED_WATERMARK WHERE id = 1")
        wm = rows[0][0] if rows else None
        if isinstance(wm, str):
            wm = date.fromisoformat(wm)
        if wm is None:
            self.main.execute(f"INSERT INTO DERIVED_WATERMARK (id, as_of) VALUES (1, DATE '{as_of.isoformat()}')")
            self._step(report, "derived_refresh", "OK", "first run: watermark set")
            return False
        if as_of < wm:
            raise ValueError(f"as-of date {as_of} is before the derived watermark {wm}; "
                             "derived columns cannot move backwards")
        if as_of == wm:
            self._step(report, "derived_refresh", "SKIPPED", "already current")
            return False
        w = sql_window_vars(wm, as_of, STALE_DAYS)
        for cv in self._country_vars({**base, **w}):
            self.run_file("13_refresh_derived.sql", cv)
        self.main.execute(f"UPDATE DERIVED_WATERMARK SET as_of = DATE '{as_of.isoformat()}', "
                          "updated_at = CURRENT_TIMESTAMP() WHERE id = 1")
        self._step(report, "derived_refresh", "OK", f"refreshed ({wm} -> {as_of})")
        return True

    # ------------------------------------------------------------------ security / retention
    def security(self) -> None:
        """Roles, masking, row access and grants (needs an admin role and Enterprise edition).
        Re-runnable: attaching a policy that is already attached is tolerated; any other error is not."""
        self._best_effort("14_security.sql", {}, {"ADD ROW ACCESS POLICY"})
        for v in self._country_vars({}):
            self._best_effort("14b_security_country.sql", v, {"DROP ROW ACCESS POLICY"})
        self._log("security applied")

    def _best_effort(self, name: str, variables: dict[str, str], tolerated: set[str]) -> None:
        for stmt in statements(name, variables, self.sql_dir):
            try:
                self.main.execute(stmt)
            except Exception as e:
                if not any(t in stmt for t in tolerated):
                    raise
                self._log(f"tolerated on re-run: {stmt.split(chr(10))[0][:70]} ({str(e)[:80]})")

    def purge(self, days: int) -> None:
        """Delete RAW / staging / reject rows older than ``days`` (they hold unmasked values)."""
        if days < 1:
            raise ValueError("retention must be at least 1 day")
        self.run_file("16_retention.sql", {"RETENTION_DAYS": str(int(days))})
        self._log(f"purged raw/staging/reject rows older than {days} day(s)")

    # ------------------------------------------------------------------ parity
    def parity(self, load_id: str, golden_dir: Path) -> list[tuple]:
        """Load the golden files and return every difference vs the SQL staging output."""
        staging, rejects = read_golden(golden_dir)
        self.run_file("15a_parity_tables.sql")
        self._insert_rows("PARITY_STG_EXPECTED", staging)
        self._insert_rows("PARITY_REJECT_EXPECTED", rejects)
        return [r for rows in self.query_file("15b_parity_compare.sql", {"LOAD_ID": load_id}) for r in rows]

    def _insert_rows(self, table: str, rows: list[dict], chunk: int = 100) -> None:
        if not rows:
            return
        cols = list(rows[0])
        for i in range(0, len(rows), chunk):
            vals = ",".join("(" + ",".join(_q(r[c]) for c in cols) + ")" for r in rows[i:i + chunk])
            self.main.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES {vals}")

    # ------------------------------------------------------------------ demo
    def demo(self, sample_dir: Path, golden_dir: Path, parallel: bool = True) -> tuple[LoadReport, list[tuple]]:
        self.deploy()
        report = self.load(
            [str(Path(sample_dir) / f) for f in SAMPLE_FILES],
            [str(Path(sample_dir) / "redemptions_20240115.json")],
            GOLDEN_AS_OF, max_error_rate=0.10, parallel=parallel)
        diffs = self.parity(report.load_id, golden_dir)
        return report, diffs


# ---------------------------------------------------------------------- real connection
class ConnectorExecutor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql: str) -> list[tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            try:
                return cur.fetchall()
            except Exception:           # statement without a result set
                return []
        finally:
            cur.close()

    def close(self) -> None:
        self.conn.close()


def connect_from_env() -> ConnectorExecutor:
    try:
        import snowflake.connector
    except ImportError as e:            # pragma: no cover
        raise SystemExit("pip install snowflake-connector-python") from e
    missing = [k for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"set environment variable(s): {', '.join(missing)} (see docs/SNOWFLAKE_RUNBOOK.md)")
    # No database/schema/warehouse here on purpose: on the very first run they do not exist yet,
    # and asking for a missing database at connect time can fail the connection. Every SQL file
    # starts with USE SCHEMA SKYPOINTS.CORE, and 00_setup.sql selects the warehouse it creates.
    kwargs = dict(account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
                  database="SKYPOINTS", schema="CORE", warehouse="SKYPOINTS_WH")
    if os.environ.get("SNOWFLAKE_ROLE"):
        kwargs["role"] = os.environ["SNOWFLAKE_ROLE"]
    if os.environ.get("SNOWFLAKE_AUTHENTICATOR"):        # e.g. externalbrowser (needs SSO configured)
        kwargs["authenticator"] = os.environ["SNOWFLAKE_AUTHENTICATOR"]
    else:
        kwargs["password"] = os.environ.get("SNOWFLAKE_PASSWORD", "")
    return ConnectorExecutor(snowflake.connector.connect(**kwargs))


WAREHOUSE = "SKYPOINTS_WH"      # created by sql/00_setup.sql


def connect_session() -> ConnectorExecutor:
    """An extra session for a parallel merge. It must select the warehouse itself: a new
    session has none unless the user happens to have a default one."""
    return connect_from_env()


def _bootstrap_runner() -> SnowflakeRunner:
    main = connect_from_env()
    # The database/schema/warehouse may not exist before the first deploy, so create them
    # on this very session first; later sessions (parallel merges) connect after deploy.
    from .sqlrender import load_sql, split_statements
    for stmt in split_statements(load_sql("00_setup.sql")):
        main.execute(stmt)
    return SnowflakeRunner(main, connect=connect_session)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SkyPoints Snowflake runner")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deploy", help="create/refresh all objects (idempotent)")
    d = sub.add_parser("demo", help="deploy, load the sample data, and diff Snowflake against the Python oracle")
    d.add_argument("--sample-dir", default=str(Path(__file__).resolve().parents[2] / "sample_data"))
    d.add_argument("--golden-dir", default=str(Path(__file__).resolve().parents[2] / "tests" / "golden"))
    ld = sub.add_parser("load", help="load member and/or redemption files")
    ld.add_argument("--members", nargs="*", default=[])
    ld.add_argument("--redemptions", nargs="*", default=[])
    ld.add_argument("--as-of", default=None)
    ld.add_argument("--snapshot", default="", help="comma list of sources whose files are FULL extracts")
    ld.add_argument("--max-error-rate", type=float, default=0.02)
    ld.add_argument("--serial", action="store_true", help="one transaction, no parallel sessions")
    ld.add_argument("--load-id", default=None)
    sub.add_parser("security", help="apply roles, masking, row access and grants (admin, Enterprise edition)")
    pg = sub.add_parser("purge", help="delete RAW/staging/reject rows older than N days")
    pg.add_argument("--days", type=int, default=30)
    pr = sub.add_parser("parity", help="diff an existing sample load against the golden files")
    pr.add_argument("--load-id", required=True)
    pr.add_argument("--golden-dir", default=str(Path(__file__).resolve().parents[2] / "tests" / "golden"))
    a = p.parse_args(argv)

    runner = _bootstrap_runner()
    try:
        if a.cmd == "deploy":
            runner.deploy()
        elif a.cmd == "security":
            runner.security()
        elif a.cmd == "purge":
            runner.purge(a.days)
        elif a.cmd == "demo":
            report, diffs = runner.demo(Path(a.sample_dir), Path(a.golden_dir), parallel=True)
            print(f"\nload {report.load_id}: raw={report.raw_rows} errors={report.error_rows} "
                  f"staged={report.staged_rows} applied={report.applied_rows}")
            if diffs:
                print(f"PARITY FAIL: {len(diffs)} difference(s) between Snowflake and the Python oracle:")
                for r in diffs:
                    print("  ", r)
                return 4
            print("PARITY OK: Snowflake staging output matches the Python implementation exactly")
        elif a.cmd == "parity":
            diffs = runner.parity(a.load_id, Path(a.golden_dir))
            for r in diffs:
                print("  ", r)
            print("PARITY OK" if not diffs else f"PARITY FAIL: {len(diffs)} difference(s)")
            return 0 if not diffs else 4
        elif a.cmd == "load":
            as_of = date.fromisoformat(a.as_of) if a.as_of else date.today()
            snaps = {s.strip().upper() for s in a.snapshot.split(",") if s.strip()}
            rep = runner.load(a.members, a.redemptions, as_of, snapshot_sources=snaps,
                              max_error_rate=a.max_error_rate, parallel=not a.serial, load_id=a.load_id)
            print(f"load {rep.load_id} complete: raw={rep.raw_rows} errors={rep.error_rows} "
                  f"staged={rep.staged_rows} applied={rep.applied_rows}")
        return 0
    except GateFailed as e:
        print(f"GATE FAIL: {e}", file=sys.stderr)
        return 2
    except ReconciliationFailed as e:
        print(f"RECONCILE FAIL: {e}", file=sys.stderr)
        return 3
    finally:
        runner.main.close()


if __name__ == "__main__":
    raise SystemExit(main())
