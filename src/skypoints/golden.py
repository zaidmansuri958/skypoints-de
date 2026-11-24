"""Golden files: what the staging step must produce for the sample data.

The Python implementation is the oracle. `tests/golden/*.csv` are generated from it, a unit
test fails if they go stale, and sql/15_parity*.sql loads them into Snowflake and diffs them
against what the SQL staging produced. A zero-row diff is the proof that the SQL and the
Python implement the same rules, so the logic existing twice cannot silently drift.

Regenerate after an intentional rule change:  python -m skypoints.golden
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .pipeline import run

GOLDEN_AS_OF = date(2026, 11, 21)
SAMPLE_FILES = ("IND.csv", "USA.csv", "AUS.xlsx", "GLOBAL_20240115.txt")

STAGING_COLUMNS = ("source_file", "source_row", "member_key", "member_name", "enrollment_date",
                   "last_flight_date", "dob", "tier_code", "state", "country_code", "post_code",
                   "is_active", "member_type", "age", "stale_member")
REJECT_COLUMNS = ("source_file", "source_row", "rule_code", "severity")

# Rules decided outside the SQL staging step: the runner writes HEADER_SPEC itself, and the
# snapshot guard is a batch decision.
_NOT_IN_PARITY = {"SNAPSHOT_GUARD"}


def _s(value: object) -> str:
    """Same text Snowflake's COALESCE(TO_VARCHAR(x), '') produces."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build(sample_dir: Path) -> tuple[list[dict], list[dict]]:
    res = run([str(Path(sample_dir) / f) for f in SAMPLE_FILES], [], GOLDEN_AS_OF)
    staging = sorted(
        ({c: _s(getattr(m, c)) for c in STAGING_COLUMNS} for m in res.staged),
        key=lambda r: (r["source_file"], int(r["source_row"])),
    )
    seen: set[tuple] = set()
    rejects = []
    for r in res.rejects:
        if r["source_system"] == "REDEMPTION" or r["rule_code"] in _NOT_IN_PARITY:
            continue
        key = (r["source_file"], _s(r["source_row"]), r["rule_code"], r["severity"])
        if key not in seen:                 # a rule can fire on several columns of one row
            seen.add(key)
            rejects.append(dict(zip(REJECT_COLUMNS, key)))
    rejects.sort(key=lambda r: (r["source_file"], int(r["source_row"]), r["rule_code"], r["severity"]))
    return staging, rejects


def _write(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def write_golden(sample_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    staging, rejects = build(sample_dir)
    _write(out_dir / "staging_expected.csv", STAGING_COLUMNS, staging)
    _write(out_dir / "rejects_expected.csv", REJECT_COLUMNS, rejects)


def read_golden(out_dir: Path) -> tuple[list[dict], list[dict]]:
    def rd(name):
        with open(Path(out_dir) / name, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    return rd("staging_expected.csv"), rd("rejects_expected.csv")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    write_golden(root / "sample_data", root / "tests" / "golden")
    print("wrote tests/golden/staging_expected.csv and rejects_expected.csv")
