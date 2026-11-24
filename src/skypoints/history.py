"""SCD Type 2 history of tracked member attributes (mirrors sql/08_history_index.sql).

One *current* row per member (valid_to None); a change in any tracked attribute closes
it and opens a new one. Country moves are derived from consecutive history rows, so the
move log can never disagree with the history.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date

from .config import TRACKED_ATTRS
from .stage import StagedMember


@dataclass
class HistoryRow:
    member_key: str
    valid_from: date
    valid_to: date | None
    is_current: bool
    country_code: str | None
    tier_code: str | None
    is_active: str | None
    state: str | None

    def as_dict(self) -> dict:
        return asdict(self)

    def attrs(self) -> tuple:
        return tuple(getattr(self, a) for a in TRACKED_ATTRS)


def effective_date(m: StagedMember) -> date:
    """The date a change takes effect: the feed date, else the run's as-of date."""
    return m.feed_date or m.as_of_date


@dataclass
class HistoryStore:
    rows: dict[str, list[HistoryRow]] = field(default_factory=dict)

    def record(self, m: StagedMember) -> bool:
        """Apply one winning record. Returns True when history changed. Idempotent."""
        eff = effective_date(m)
        attrs = tuple(getattr(m, a) for a in TRACKED_ATTRS)
        chain = self.rows.setdefault(m.member_key, [])
        current = chain[-1] if chain and chain[-1].is_current else None
        if current is not None and current.attrs() == attrs:
            return False                                  # nothing tracked changed
        if current is not None:
            current.valid_to = eff                        # close the old version
            current.is_current = False
        chain.append(HistoryRow(m.member_key, eff, None, True, *attrs))
        return True

    def current(self, key: str) -> HistoryRow | None:
        chain = self.rows.get(key, [])
        return chain[-1] if chain and chain[-1].is_current else None

    def as_of(self, key: str, when: date) -> HistoryRow | None:
        """The version that was valid on ``when`` (valid_from <= when < valid_to)."""
        for r in self.rows.get(key, []):
            if r.valid_from <= when and (r.valid_to is None or when < r.valid_to):
                return r
        return None

    def moves(self) -> list[dict]:
        """Country moves, derived from consecutive history rows (mirrors V_MEMBER_MOVES)."""
        out = []
        for key, chain in self.rows.items():
            for prev, cur in zip(chain, chain[1:]):
                if prev.country_code != cur.country_code:
                    out.append({"member_key": key, "from_country": prev.country_code,
                                "to_country": cur.country_code, "moved_on": cur.valid_from})
        return sorted(out, key=lambda r: (r["moved_on"], r["member_key"]))

    def flat_rows(self) -> list[dict]:
        return [r.as_dict() for chain in self.rows.values() for r in chain]
