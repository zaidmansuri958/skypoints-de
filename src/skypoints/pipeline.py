"""End-to-end local run: files -> stage -> validate -> GATE -> latest-wins -> per-country tables
-> snapshot soft-delete -> redemptions -> derived refresh -> identity candidates -> reconcile.

Usage: python -m skypoints.pipeline --members a.csv b.xlsx GLOBAL_20240115.txt \
           --redemptions r.json --out out/ [--as-of 2026-11-21] [--max-error-rate 0.02] \
           [--snapshot IND,USA]
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import FEED_MODE_DEFAULT, SNAPSHOT_DROP_GUARD, SOURCES, configured_snapshot_sources
from .derived import refresh_derived
from .identity import match_candidates
from .normalize import clean
from .readers import open_source
from .redemptions import RedemptionStore, flatten, join_to_members
from .stage import ERROR, WARNING, Issue, StagedMember, stage_row
from .transform import CountryStore, ReconciliationError, latest_wins
from .validate import duplicate_key_issues, header_issues, id_length_issues


@dataclass
class RunResult:
    store: CountryStore
    redemptions: RedemptionStore
    rejects: list[dict] = field(default_factory=list)
    staged: list[StagedMember] = field(default_factory=list)   # every staged row, before dedupe
    total_rows: int = 0            # member rows + redemption transactions, for reporting
    error_rows: int = 0
    member_rows: int = 0           # the gate looks at member rows only
    member_error_rows: int = 0
    gate_failed: bool = False
    snapshot_marked: dict[str, int] = field(default_factory=dict)
    derived_refreshed: int = 0
    orphans_linked: int = 0
    match_candidates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.error_rows / self.total_rows if self.total_rows else 0.0

    @property
    def member_error_rate(self) -> float:
        return self.member_error_rows / self.member_rows if self.member_rows else 0.0


def _reject(source_system, source_file, source_row, member_key, issue: Issue) -> dict:
    return {
        "source_system": source_system, "source_file": source_file, "source_row": source_row,
        "member_key": member_key, "rule_code": issue.rule_code, "severity": issue.severity,
        "column_name": issue.column_name, "bad_value": issue.bad_value, "reason": issue.reason,
    }


def run(member_files: list[str], redemption_files: list[str], as_of: date,
        store: CountryStore | None = None,
        redemption_store: RedemptionStore | None = None,
        snapshot_sources: tuple[str, ...] | set[str] = (),
        max_error_rate: float | None = None,
        snapshot_guard: float = SNAPSHOT_DROP_GUARD) -> RunResult:
    store = store if store is not None else CountryStore()
    result = RunResult(store=store,
                       redemptions=redemption_store if redemption_store is not None else RedemptionStore())
    snapshot_sources = {s.upper() for s in snapshot_sources}
    unknown = snapshot_sources - set(SOURCES)
    if unknown:
        raise ValueError(f"unknown snapshot source(s): {sorted(unknown)}")
    staged = result.staged
    seen: dict[str, set[str]] = {}          # source -> every member key present in its files
    feed_dates: dict[str, date] = {}

    # 1. read + stage every member row; collect row-level rejects
    for path in member_files:
        src = open_source(path)
        for issue in header_issues(src):
            result.rejects.append(_reject(src.source_system, src.path.name, 0, None, issue))
        for raw in src.rows():
            result.total_rows += 1
            result.member_rows += 1
            mid = clean(raw.values.get("member_id"))
            if mid:      # "seen" includes rows that fail validation: a bad row is not a departed member
                seen.setdefault(raw.source_system, set()).add(f"{raw.source_system}|{mid}")
            member, issues = stage_row(raw, as_of)
            for i in issues:
                result.rejects.append(_reject(raw.source_system, raw.source_file, raw.source_row,
                                              f"{raw.source_system}|{mid}", i))
            if member is None:
                result.error_rows += 1
                result.member_error_rows += 1
            else:
                staged.append(member)
            if raw.feed_date:
                feed_dates[raw.source_system] = max(raw.feed_date, feed_dates.get(raw.source_system, raw.feed_date))

    # 2. batch-level rules (need the whole batch)
    for check in (duplicate_key_issues(staged), id_length_issues(staged)):
        for m in staged:
            issue = check.get((m.source_file, m.source_row))
            if issue:
                result.rejects.append(_reject(m.source_system, m.source_file, m.source_row, m.member_key, issue))

    # 3. GATE: a bad batch is stopped BEFORE anything is loaded, not reported afterwards
    if max_error_rate is not None and result.member_error_rate > max_error_rate:
        result.gate_failed = True
        result.notes.append(f"gate failed: member error rate {result.member_error_rate:.1%} "
                            f"exceeds {max_error_rate:.1%}; nothing was loaded")
        return result

    # 4. latest wins -> per-country tables (+ history, index)
    store.apply(latest_wins(staged))

    # 5. snapshot sources: members the full extract no longer lists are soft-deleted
    for system in sorted(snapshot_sources & seen.keys()):
        marked, why = store.mark_missing(system, seen[system], feed_dates.get(system, as_of), snapshot_guard)
        result.snapshot_marked[system] = marked
        if why:
            result.notes.append(f"snapshot {system}: {why}")
            result.rejects.append(_reject(system, "*", 0, None, Issue("SNAPSHOT_GUARD", WARNING, None, None, why)))

    # 6. redemptions: flatten, validate, merge on txn_id
    for path in redemption_files:
        for red, issues, line in flatten(path):
            result.total_rows += 1
            for i in issues:
                result.rejects.append(_reject("REDEMPTION", Path(path).name, line, red.txn_id if red else None, i))
            if red is None:
                result.error_rows += 1
            else:
                result.redemptions.merge(red)
    # link redemptions to members; also picks up older orphans whose member just arrived
    result.orphans_linked = result.redemptions.resolve(store)

    # 7. keep age / stale_member current through as_of, touching only rows that changed
    result.derived_refreshed = refresh_derived(store, as_of)

    # 8. cross-source identity candidates (proposals only; nothing is merged)
    result.match_candidates, notes = match_candidates(store)
    result.notes.extend(notes)

    # 9. reconcile: the index, tables and history must agree before the load counts as done
    problems = store.reconcile()
    if problems:
        raise ReconciliationError(f"{len(problems)} inconsistencies, first: {problems[0]}")
    return result


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_outputs(result: RunResult, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for country, table in result.store.tables.items():
        _write_csv(out / f"MEMBER_{country}.csv", [m.as_dict() for m in table.values()])
    _write_csv(out / "STG_REJECT.csv", result.rejects)
    _write_csv(out / "REDEMPTION_FACT.csv", [r.as_dict() for r in result.redemptions.facts.values()])
    _write_csv(out / "REDEMPTION_WITH_MEMBER.csv", join_to_members(result.redemptions, result.store))
    _write_csv(out / "MEMBER_HISTORY.csv", result.store.history.flat_rows())
    _write_csv(out / "V_MEMBER_MOVES.csv", result.store.history.moves())
    _write_csv(out / "V_MEMBER_MATCH_CANDIDATES.csv", result.match_candidates)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SkyPoints member pipeline (reference implementation)")
    p.add_argument("--members", nargs="+", required=True)
    p.add_argument("--redemptions", nargs="*", default=[])
    p.add_argument("--out", default="out")
    p.add_argument("--as-of", default=None, help="YYYY-MM-DD; defaults to today")
    p.add_argument("--max-error-rate", type=float, default=0.02)
    p.add_argument("--snapshot", default="",
                   help=f"comma list of sources whose files are FULL extracts (default feed mode is "
                        f"'{FEED_MODE_DEFAULT}'); members they no longer list are soft-deleted")
    a = p.parse_args(argv)

    as_of = date.fromisoformat(a.as_of) if a.as_of else date.today()
    snaps = tuple(configured_snapshot_sources() | {s.strip().upper() for s in a.snapshot.split(",") if s.strip()})
    try:
        result = run(a.members, a.redemptions, as_of, snapshot_sources=snaps, max_error_rate=a.max_error_rate)
    except ReconciliationError as e:
        print(f"RECONCILE FAIL: {e}", file=sys.stderr)
        return 3
    write_outputs(result, Path(a.out))

    loaded = sum(len(t) for t in result.store.tables.values())
    print(f"rows read: {result.total_rows}  error rows: {result.error_rows} ({result.error_rate:.1%})")
    print(f"members loaded: {loaded}  moved countries: {len(result.store.moved)}  "
          f"redemptions: {len(result.redemptions.facts)} (orphans: {len(result.redemptions.orphans())})  "
          f"reject entries: {len(result.rejects)}")
    for c, t in result.store.tables.items():
        if t:
            print(f"  MEMBER_{c}: {len(t)}")
    if result.snapshot_marked:
        print(f"  soft-deleted (missing from snapshot): {result.snapshot_marked}")
    if result.match_candidates:
        print(f"  cross-source match candidates: {len(result.match_candidates)}")
    for n in result.notes:
        print(f"  note: {n}")
    if result.gate_failed:
        print(f"GATE FAIL: member error rate {result.member_error_rate:.1%} exceeds {a.max_error_rate:.1%}; "
              "nothing was loaded", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
