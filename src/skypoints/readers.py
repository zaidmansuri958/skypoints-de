"""Streaming file readers. Each yields raw rows as dicts of canonical column -> string.

Rows are produced lazily (generators), so memory stays flat regardless of file size.
.xlsx is read with openpyxl read-only mode; at production scale it is converted to
CSV/Parquet at the edge because xlsx is not splittable.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .config import SOURCES, SPEC_COLUMNS


@dataclass
class RawRow:
    source_system: str
    source_file: str
    source_row: int                       # 1-based data row number (after header)
    feed_date: date | None
    values: dict[str, str | None]
    shape_error: str | None = None        # set when a pipe row has the wrong field count


@dataclass
class SourceFile:
    path: Path
    source_system: str
    feed_date: date | None
    missing_spec_columns: list[str] = field(default_factory=list)

    def rows(self) -> Iterator[RawRow]:
        raise NotImplementedError


def detect_source(path: Path) -> tuple[str, date | None]:
    """Source system and feed date from the file name, e.g. IND.csv, GLOBAL_20240115.txt."""
    stem = path.stem.upper()
    system = stem.split("_")[0]
    if system not in SOURCES:
        raise ValueError(f"cannot determine source system for {path.name}; expected one of {sorted(SOURCES)}")
    m = re.search(r"(\d{8})", stem)
    feed = datetime.strptime(m.group(1), "%Y%m%d").date() if m else None
    return system, feed


def _map_headers(headers: list[str], system: str) -> list[str | None]:
    mapping = {k.strip().lower(): v for k, v in SOURCES[system]["columns"].items()}
    return [mapping.get(h.strip().lower()) for h in headers]


def _cell(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class _CsvFile(SourceFile):
    def rows(self) -> Iterator[RawRow]:
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            headers = _map_headers(next(reader), self.source_system)
            for n, rec in enumerate(reader, start=1):
                if not any(c.strip() for c in rec):
                    continue
                vals = {h: _cell(c) for h, c in zip(headers, rec) if h}
                yield RawRow(self.source_system, self.path.name, n, self.feed_date, vals)


class _XlsxFile(SourceFile):
    def rows(self) -> Iterator[RawRow]:
        from openpyxl import load_workbook

        wb = load_workbook(self.path, read_only=True, data_only=True)
        try:
            it = wb.active.iter_rows(values_only=True)
            headers = _map_headers([str(h) for h in next(it)], self.source_system)
            for n, rec in enumerate(it, start=1):
                if rec is None or all(c is None for c in rec):
                    continue
                vals = {h: _cell(c) for h, c in zip(headers, rec) if h}
                yield RawRow(self.source_system, self.path.name, n, self.feed_date, vals)
        finally:
            wb.close()


class _PipeFile(SourceFile):
    """|H|col|col... header then |D|v|v... detail records (the GLOBAL design-spec feed)."""

    def _split(self, line: str) -> list[str]:
        # strip the leading '|' marker; fields separated by '|'
        return line.rstrip("\r\n").split("|")[1:]

    def rows(self) -> Iterator[RawRow]:
        headers: list[str | None] = []
        n = 0
        with open(self.path, encoding="utf-8-sig") as fh:
            for line in fh:
                if not line.strip():
                    continue
                parts = self._split(line)
                tag, fields = parts[0], parts[1:]
                if tag == "H":
                    headers = _map_headers(fields, self.source_system)
                    continue
                if tag != "D":
                    continue
                n += 1
                err = None
                if len(fields) != len(headers):
                    err = f"expected {len(headers)} fields, got {len(fields)}"
                vals = {h: _cell(c) for h, c in zip(headers, fields) if h}
                yield RawRow(self.source_system, self.path.name, n, self.feed_date, vals, err)


def _peek_pipe_header(path: Path, system: str) -> list[str]:
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            if line.startswith("|H|"):
                return [h for h in _map_headers(line.rstrip("\r\n").split("|")[2:], system) if h]
    return []


def open_source(path: str | Path) -> SourceFile:
    path = Path(path)
    system, feed = detect_source(path)
    ext = path.suffix.lower()
    if ext == ".csv":
        return _CsvFile(path, system, feed)
    if ext in (".xlsx", ".xlsm"):
        return _XlsxFile(path, system, feed)
    if ext in (".txt", ".dat", ".psv"):
        sf = _PipeFile(path, system, feed)
        present = set(_peek_pipe_header(path, system))
        sf.missing_spec_columns = [c for c in SPEC_COLUMNS if c not in present]
        return sf
    raise ValueError(f"unsupported file type: {path.name}")
