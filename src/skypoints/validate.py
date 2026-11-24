"""File/batch-level validations (need to see more than one row).

Mirrors the DUP_KEY, ID_FORMAT (length norm) and HEADER_SPEC rules in sql/06_validations.sql.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from .readers import SourceFile
from .stage import WARNING, Issue, StagedMember


def header_issues(src: SourceFile) -> list[Issue]:
    """Spec says 11 columns for the GLOBAL feed; flag any the file header does not carry."""
    if not src.missing_spec_columns:
        return []
    cols = ", ".join(src.missing_spec_columns)
    return [Issue("HEADER_SPEC", WARNING, None, cols, f"file header is missing spec column(s): {cols}")]


def duplicate_key_issues(members: Iterable[StagedMember]) -> dict[tuple[str, int], Issue]:
    """Rows sharing a member_key within the batch. Returns {(source_file, source_row): Issue}.

    The latest-wins step still keeps one row; this makes the duplication visible.
    """
    members = list(members)
    counts = Counter(m.member_key for m in members)
    return {
        (m.source_file, m.source_row): Issue(
            "DUP_KEY", WARNING, "member_key", m.member_key, "duplicate member in batch; latest record wins"
        )
        for m in members
        if counts[m.member_key] > 1
    }


def id_length_issues(members: Iterable[StagedMember]) -> dict[tuple[str, int], Issue]:
    """GLOBAL feed only: flag ids whose length differs from the file's most common length."""
    by_file: dict[str, list[StagedMember]] = {}
    for m in members:
        if m.source_system == "GLOBAL":
            by_file.setdefault(m.source_file, []).append(m)
    out: dict[tuple[str, int], Issue] = {}
    for f, ms in by_file.items():
        # most common length; ties go to the longer one (deterministic, and the SQL does the same)
        norm = max(Counter(len(m.source_member_id) for m in ms).items(), key=lambda kv: (kv[1], kv[0]))[0]
        for m in ms:
            if len(m.source_member_id) != norm:
                out[(f, m.source_row)] = Issue(
                    "ID_FORMAT", WARNING, "member_id", m.source_member_id,
                    f"member id length {len(m.source_member_id)} differs from file norm {norm} (possible truncation)",
                )
    return out
