"""Partner redemption feed: flatten NDJSON, validate, merge on txn_id, join to members.

Mirrors sql/05_redemptions.sql (LATERAL FLATTEN + MERGE + join via MEMBER_INDEX).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .config import REDEMPTION_DEFAULT_SOURCE, REDEMPTION_STATUSES, SOURCES
from .stage import ERROR, WARNING, Issue
from .transform import CountryStore


@dataclass
class Redemption:
    txn_id: str
    member_id: str
    txn_date: date | None
    partner: str | None
    miles_redeemed: int | None
    status: str | None
    feed_date: date | None
    source_file: str
    source_system: str = REDEMPTION_DEFAULT_SOURCE   # id namespace of member_id
    member_key: str | None = None                    # resolved link to the member; None = orphan

    def as_dict(self) -> dict:
        return asdict(self)


def _ymd(value: object) -> date | None:
    s = str(value).strip() if value is not None else ""
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    return None


def flatten(path: str | Path) -> Iterator[tuple[Redemption | None, list[Issue], int]]:
    """Yield (redemption or None, issues, doc_line) for every transaction in an NDJSON file.

    Streams line by line, so a file of any size uses constant memory. A line that is not
    valid JSON yields one ERROR issue and no redemption.
    """
    path = Path(path)
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as e:
                yield None, [Issue("JSON_PARSE", ERROR, None, line[:80], f"invalid JSON: {e.msg}")], line_no
                continue
            member_id = doc.get("member_id")
            feed = _ymd(doc.get("feed_date"))
            # Optional namespace of member_id. Today's partner feed has none (-> GLOBAL); a
            # feed that adds it can reference the per-country id spaces with no code change.
            system = str(doc.get("source_system") or REDEMPTION_DEFAULT_SOURCE).strip().upper()
            if system not in SOURCES:
                yield None, [Issue("UNKNOWN_SOURCE", ERROR, "source_system", system,
                                   "source_system is not a known member source")], line_no
                continue
            txns = doc.get("redemptions")
            if not isinstance(txns, list):
                yield None, [Issue("JSON_SHAPE", ERROR, "redemptions", None, "redemptions must be an array")], line_no
                continue
            for t in txns:
                issues: list[Issue] = []
                txn_id = (str(t.get("txn_id")).strip() if t.get("txn_id") is not None else "")
                if not txn_id:
                    issues.append(Issue("MANDATORY", ERROR, "txn_id", None, "txn_id is required"))
                if member_id is None or not str(member_id).strip():
                    issues.append(Issue("MANDATORY", ERROR, "member_id", None, "member_id is required"))
                txn_date = _ymd(t.get("txn_date"))
                if txn_date is None:
                    issues.append(Issue("BAD_DATE", ERROR, "txn_date", t.get("txn_date"), "txn_date missing or invalid"))
                elif feed and txn_date > feed:
                    issues.append(Issue("DATE_ORDER", WARNING, "txn_date", str(txn_date), "txn_date is after feed_date"))
                miles = t.get("miles_redeemed")
                if isinstance(miles, bool) or not isinstance(miles, (int, float)) or miles != int(miles) or miles <= 0:
                    issues.append(Issue("MILES", ERROR, "miles_redeemed", miles, "miles_redeemed must be a positive integer"))
                status = str(t.get("status") or "").upper() or None
                if status not in REDEMPTION_STATUSES:
                    issues.append(Issue("STATUS", WARNING, "status", status, f"status not in {sorted(REDEMPTION_STATUSES)}"))
                if any(i.severity == ERROR for i in issues):
                    yield None, issues, line_no
                    continue
                yield Redemption(
                    txn_id=txn_id, member_id=str(member_id).strip(), txn_date=txn_date,
                    partner=t.get("partner"), miles_redeemed=int(miles), status=status,
                    feed_date=feed, source_file=path.name, source_system=system,
                ), issues, line_no


@dataclass
class RedemptionStore:
    """In-memory stand-in for REDEMPTION_FACT: merge on txn_id, newest feed_date wins."""

    facts: dict[str, Redemption] = field(default_factory=dict)

    def merge(self, r: Redemption) -> None:
        cur = self.facts.get(r.txn_id)
        if cur is None or cur.feed_date is None or (r.feed_date and r.feed_date >= cur.feed_date):
            self.facts[r.txn_id] = r

    def resolve(self, members: CountryStore) -> int:
        """Link orphans to members. Idempotent and cheap to re-run on every load: only facts
        with no member_key are examined, so a redemption that arrived before its member is
        picked up by a later member load. Returns how many were newly linked."""
        linked = 0
        for r in self.facts.values():
            if r.member_key is None:
                key = f"{r.source_system}|{r.member_id}"
                if members.get(key) is not None:
                    r.member_key = key
                    linked += 1
        return linked

    def orphans(self) -> list[Redemption]:
        return [r for r in self.facts.values() if r.member_key is None]


def join_to_members(store: RedemptionStore, members: CountryStore) -> list[dict]:
    """Facts joined to the member's CURRENT profile through the stable member_key.

    The country is looked up live, so a member who moves never leaves a stale country on a
    redemption. Orphans come back with member_key None so they can be reported.
    """
    rows = []
    for r in store.facts.values():
        m = members.get(r.member_key) if r.member_key else None
        row = r.as_dict()
        row.update(
            country_code=m.country_code if m else None,
            member_name=m.member_name if m else None,
            tier_code=m.tier_code if m else None,
            record_status=m.record_status if m else None,
        )
        rows.append(row)
    return rows
