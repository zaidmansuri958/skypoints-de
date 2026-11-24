"""Edge converter: any source file -> one canonical, streamable CSV for COPY INTO.

Why: the four sources differ in format (csv, xlsx, pipe-delimited) and column names, and
.xlsx can be neither split nor streamed by Snowflake. The edge maps every source to the
RAW_MEMBER_FILE column order, one row at a time (constant memory), so a single COPY works
for all of them. In production the same output is what Snowpipe auto-ingests from the stage.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .readers import open_source
from .stage import Issue
from .validate import header_issues

# Order of the columns in RAW_MEMBER_FILE (sql/01_ddl_raw_staging.sql), minus loaded_at.
RAW_COLUMNS = (
    "load_id", "source_system", "source_file", "source_row", "shape_error", "feed_date",
    "member_name", "member_id", "enrollment_date", "last_flight_date", "tier_code", "agent_name",
    "state", "country", "post_code", "dob", "is_active", "member_type",
)
_VALUE_COLUMNS = RAW_COLUMNS[6:]


@dataclass
class EdgeResult:
    rows: int = 0
    files: list[str] = field(default_factory=list)
    header_issues: list[tuple[str, str, Issue]] = field(default_factory=list)   # (system, file, issue)
    feed_dates: dict[str, date] = field(default_factory=dict)                   # newest per source
    sources: set[str] = field(default_factory=set)


def write_canonical_csv(files: list[str], out_path: Path, load_id: str) -> EdgeResult:
    result = EdgeResult()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(RAW_COLUMNS)
        for path in files:
            src = open_source(path)
            result.files.append(src.path.name)
            result.sources.add(src.source_system)
            for issue in header_issues(src):
                result.header_issues.append((src.source_system, src.path.name, issue))
            for raw in src.rows():
                result.rows += 1
                if raw.feed_date:
                    prev = result.feed_dates.get(raw.source_system)
                    result.feed_dates[raw.source_system] = max(raw.feed_date, prev) if prev else raw.feed_date
                w.writerow([
                    load_id, raw.source_system, raw.source_file, raw.source_row, raw.shape_error or "",
                    raw.feed_date.isoformat() if raw.feed_date else "",
                    *("" if raw.values.get(c) is None else raw.values[c] for c in _VALUE_COLUMNS),
                ])
    return result
