"""Latest-record-wins and the per-country split (mirrors sql/04_country_split.sql)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from .config import COUNTRIES
from .history import HistoryStore
from .stage import StagedMember

_MIN = date.min


def _rank(m: StagedMember) -> tuple:
    """Higher tuple wins: newest feed_date, newest last_flight_date, last row, then file name (deterministic)."""
    return (m.feed_date or _MIN, m.last_flight_date or _MIN, m.source_row, m.source_file)


def latest_wins(members: Iterable[StagedMember]) -> dict[str, StagedMember]:
    """One winning record per member_key. Streams input; holds only one record per key."""
    winners: dict[str, StagedMember] = {}
    for m in members:
        cur = winners.get(m.member_key)
        if cur is None or _rank(m) >= _rank(cur):
            winners[m.member_key] = m
    return winners


class ReconciliationError(RuntimeError):
    """The index, country tables and history disagree; the load must not be published."""


@dataclass
class CountryStore:
    """In-memory stand-in for the per-country tables plus MEMBER_INDEX."""

    tables: dict[str, dict[str, StagedMember]] = field(default_factory=lambda: {c: {} for c in COUNTRIES})
    index: dict[str, tuple[str, date | None]] = field(default_factory=dict)  # key -> (country, feed_date)
    moved: list[tuple[str, str, str]] = field(default_factory=list)          # (key, from, to)
    history: HistoryStore = field(default_factory=HistoryStore)
    derived_watermark: date | None = None   # date through which age/stale are correct everywhere

    def apply(self, winners: dict[str, StagedMember]) -> None:
        for key, m in winners.items():
            prev = self.index.get(key)
            # an older/replayed file must not overwrite a newer stored record
            if prev and m.feed_date and prev[1] and m.feed_date < prev[1]:
                continue
            country = m.country_code
            if country not in self.tables:
                raise KeyError(f"country {country} has no target table; add it to the registry")
            if prev and prev[0] != country:
                self.tables[prev[0]].pop(key, None)          # remove from the old country
                self.moved.append((key, prev[0], country))
            self.tables[country][key] = m                    # upsert into the new one
            self.index[key] = (country, m.feed_date)
            self.history.record(m)                           # SCD2: tier/country/... changes

    # -- lifecycle ---------------------------------------------------------
    def active_keys(self, source_system: str) -> list[str]:
        prefix = f"{source_system}|"
        return [k for t in self.tables.values() for k, m in t.items()
                if k.startswith(prefix) and m.record_status == "ACTIVE"]

    def mark_missing(self, source_system: str, seen_keys: set[str], feed_date: date,
                     guard: float) -> tuple[int, str | None]:
        """Soft-delete members of ``source_system`` that a FULL snapshot no longer lists.

        ``seen_keys`` must contain every key present in the file, including rows that failed
        validation (a bad row is not a departed member). Returns (marked, skip_reason).
        Nothing is marked when the snapshot looks truncated (guard) or is empty.
        """
        active = self.active_keys(source_system)
        if not seen_keys:
            return 0, "snapshot file contained no members; refusing to mark everyone missing"
        if active and len(seen_keys) < (1 - guard) * len(active):
            return 0, (f"snapshot lists {len(seen_keys)} members but {len(active)} are active "
                       f"(more than {guard:.0%} drop); treated as a partial file")
        marked = 0
        for key in active:
            if key not in seen_keys:
                m = self.tables[self.index[key][0]][key]
                m.record_status = "MISSING_FROM_FEED"
                m.missing_since = feed_date
                marked += 1
        return marked, None

    def get(self, member_key: str) -> StagedMember | None:
        hit = self.index.get(member_key)
        return self.tables[hit[0]][member_key] if hit and member_key in self.tables[hit[0]] else None

    def reconcile(self) -> list[str]:
        """Prove the routing index, country tables and history agree (mirrors sql/12_reconcile.sql).

        MEMBER_INDEX is a derived structure; this is the check that it never silently drifts.
        Returns human-readable problems; an empty list means consistent.
        """
        problems: list[str] = []
        seen_in: dict[str, list[str]] = {}
        for country, table in self.tables.items():
            for key, m in table.items():
                seen_in.setdefault(key, []).append(country)
                if m.country_code != country:
                    problems.append(f"{key}: stored in MEMBER_{country} but country_code is {m.country_code}")
        for key, countries in seen_in.items():
            if len(countries) > 1:
                problems.append(f"{key}: present in several country tables {sorted(countries)}")
            hit = self.index.get(key)
            if hit is None:
                problems.append(f"{key}: in MEMBER_{countries[0]} but missing from the index")
            elif hit[0] not in countries:
                problems.append(f"{key}: index says {hit[0]} but row is in {sorted(countries)}")
        for key, (country, _) in self.index.items():
            if key not in self.tables.get(country, {}):
                problems.append(f"{key}: index points to {country} but no row exists there")
            cur = self.history.current(key)
            if cur is None:
                problems.append(f"{key}: no current history row")
            elif cur.country_code != country:
                problems.append(f"{key}: history says {cur.country_code} but index says {country}")
        return problems

    def lookup(self, source_system: str, member_id: str) -> StagedMember | None:
        key = f"{source_system}|{member_id}"
        hit = self.index.get(key)
        return self.tables[hit[0]][key] if hit else None
