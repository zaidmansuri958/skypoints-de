"""Cross-source identity candidates (mirrors sql/11_identity.sql).

The composite member_key (source|id) is safe but treats one real person who appears in two
sources as two members. This module PROPOSES matches; it never merges. A wrong merge (two
different people fused) is far worse than a missed one (one person listed twice), so a human
confirms candidates into MEMBER_XREF.

Blocking key: normalised name + date of birth. Only pairs from DIFFERENT source systems
are proposed (inside one source the id already identifies the member).
Limitation: normalisation is ASCII-only (accents are dropped), identical in SQL and Python.
"""
from __future__ import annotations

import re
from collections import defaultdict
from itertools import combinations

from .transform import CountryStore

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
MAX_GROUP = 20     # a block bigger than this is a common name+dob, too ambiguous to propose


def norm_name(name: str | None) -> str:
    return _NON_ALNUM.sub(" ", (name or "").lower()).strip()


def match_candidates(store: CountryStore, max_group: int = MAX_GROUP) -> tuple[list[dict], list[str]]:
    """Return (candidate pairs, notes about skipped over-large blocks)."""
    blocks: dict[tuple, list] = defaultdict(list)
    for table in store.tables.values():
        for m in table.values():
            n = norm_name(m.member_name)
            if m.dob is not None and n:
                blocks[(n, m.dob)].append(m)

    out: list[dict] = []
    notes: list[str] = []
    for (name, dob), members in blocks.items():
        if len({m.member_key for m in members}) < 2:
            continue
        if len(members) > max_group:
            notes.append(f"skipped block '{name}' {dob}: {len(members)} members (ambiguous)")
            continue
        for a, b in combinations(sorted(members, key=lambda x: x.member_key), 2):
            if a.source_system == b.source_system:
                continue
            same_enroll = a.enrollment_date is not None and a.enrollment_date == b.enrollment_date
            out.append({
                "member_key_a": a.member_key, "member_key_b": b.member_key,
                "name_norm": name, "dob": dob,
                "confidence": "HIGH" if same_enroll else "MEDIUM",
                "rule": "NAME_DOB_ENROLL" if same_enroll else "NAME_DOB",
            })
    out.sort(key=lambda r: (r["member_key_a"], r["member_key_b"]))
    return out, notes
