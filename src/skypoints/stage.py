"""Row-level staging: parse/normalize one raw row, derive columns, apply row validations.

Mirrors sql/03_load_staging.sql (typing + derived columns) and the row-level rules of
sql/06_validations.sql. File-level rules (duplicates, id length norm) live in validate.py.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date

from .config import ACTIVE_FLAGS, MAX_AGE, MAX_POST_CODE_DIGITS, SOURCES, SPEC_LENGTHS, TIER_CODES
from .normalize import BAD, EMPTY, clean, compute_age, is_stale, normalize_country, parse_date
from .readers import RawRow

ERROR, WARNING = "ERROR", "WARNING"


@dataclass(frozen=True)
class Issue:
    rule_code: str
    severity: str
    column_name: str | None
    bad_value: str | None
    reason: str


@dataclass
class StagedMember:
    member_key: str
    source_system: str
    source_member_id: str
    member_name: str
    enrollment_date: date | None
    last_flight_date: date | None
    tier_code: str | None
    agent_name: str | None
    state: str | None
    country_code: str | None
    post_code: int | None
    dob: date | None
    is_active: str | None
    member_type: str | None
    as_of_date: date
    age: int | None
    stale_member: bool
    feed_date: date | None
    source_file: str
    source_row: int
    # lifecycle (see CountryStore.mark_missing): ACTIVE, or MISSING_FROM_FEED once a
    # full-snapshot feed no longer lists the member.
    record_status: str = "ACTIVE"
    last_seen_date: date | None = None
    missing_since: date | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def stage_row(raw: RawRow, as_of: date) -> tuple[StagedMember | None, list[Issue]]:
    """Return (staged record or None if an ERROR rule failed, issues)."""
    issues: list[Issue] = []
    v = raw.values
    cfg = SOURCES[raw.source_system]
    fmts = cfg["date_formats"]

    if raw.shape_error:
        issues.append(Issue("ROW_SHAPE", ERROR, None, None, raw.shape_error))

    name = clean(v.get("member_name"))
    mid = clean(v.get("member_id"))
    if name is None:
        issues.append(Issue("MANDATORY", ERROR, "member_name", v.get("member_name"), "member name is required"))
    if mid is None:
        issues.append(Issue("MANDATORY", ERROR, "member_id", v.get("member_id"), "member id is required"))
    elif not mid.isdigit():
        issues.append(Issue("ID_FORMAT", WARNING, "member_id", mid, "member id not numeric"))

    dates: dict[str, date | None] = {}
    for col in ("enrollment_date", "last_flight_date", "dob"):
        d, status = parse_date(v.get(col), fmts[col]) if v.get(col) is not None else (None, EMPTY)
        dates[col] = d
        if status == EMPTY and col == "enrollment_date":
            issues.append(Issue("MANDATORY", ERROR, col, v.get(col), "enrollment date is required"))
        elif status == BAD:
            sev = ERROR if col == "enrollment_date" else WARNING
            issues.append(Issue("BAD_DATE", sev, col, v.get(col), f"{col} is not a valid date"))

    enroll, flight, dob = dates["enrollment_date"], dates["last_flight_date"], dates["dob"]
    if enroll and enroll > as_of:
        issues.append(Issue("DATE_ORDER", ERROR, "enrollment_date", str(enroll), "enrollment date is in the future"))
    if enroll and flight and flight < enroll:
        issues.append(Issue("DATE_ORDER", WARNING, "last_flight_date", str(flight), "flight date is before enrollment date"))
    if dob and dob > as_of:
        issues.append(Issue("DATE_ORDER", WARNING, "dob", str(dob), "date of birth is in the future"))
    age = compute_age(dob, as_of)
    if age is not None and (age < 0 or age > MAX_AGE):
        issues.append(Issue("AGE_RANGE", WARNING, "dob", str(dob), f"age {age} outside 0-{MAX_AGE}"))

    # country: fixed per-country file, or looked up from the Country column (GLOBAL)
    country, aliased = (cfg["fixed_country"], False) if cfg["fixed_country"] else normalize_country(v.get("country"))
    raw_country = clean(v.get("country"))
    if country is None:
        issues.append(Issue("UNKNOWN_COUNTRY", ERROR, "country", raw_country, "country not in registry"))
    elif aliased:
        issues.append(Issue("COUNTRY_ALIAS", WARNING, "country", raw_country, f"non-ISO code normalized to {country}"))

    tier = clean(v.get("tier_code"))
    tier = tier.upper() if tier else None
    if tier and tier not in TIER_CODES:
        issues.append(Issue("TIER_CODE", WARNING, "tier_code", tier, f"tier code outside {sorted(TIER_CODES)}"))
    active = clean(v.get("is_active"))
    active = active.upper() if active else None
    if active and active not in ACTIVE_FLAGS:
        issues.append(Issue("ACTIVE_FLAG", WARNING, "is_active", active, "active flag not in A/I"))

    post = clean(v.get("post_code"))
    post_i = int(post) if post and post.isdigit() and len(post) <= MAX_POST_CODE_DIGITS else None
    if post and post_i is None:
        issues.append(Issue("POST_CODE", WARNING, "post_code", post,
                            f"post code must be 1-{MAX_POST_CODE_DIGITS} digits"))

    state = (clean(v.get("state")) or "").upper() or None
    agent = clean(v.get("agent_name"))
    for col, val in (("member_name", name), ("member_id", mid), ("tier_code", tier),
                     ("state", state), ("agent_name", agent)):
        if val is not None and len(val) > SPEC_LENGTHS[col]:
            issues.append(Issue("FIELD_LENGTH", WARNING, col, val,
                                f"{col} is longer than the spec length {SPEC_LENGTHS[col]}"))

    if any(i.severity == ERROR for i in issues):
        return None, issues

    member = StagedMember(
        member_key=f"{raw.source_system}|{mid}",
        source_system=raw.source_system,
        source_member_id=mid,
        member_name=name,
        enrollment_date=enroll,
        last_flight_date=flight,
        tier_code=tier,
        agent_name=agent,
        state=state,
        country_code=country,
        post_code=post_i,
        dob=dob,
        is_active=active,
        member_type=(clean(v.get("member_type")) or "").upper() or None,
        as_of_date=as_of,
        age=age,
        stale_member=is_stale(flight, as_of),
        feed_date=raw.feed_date,
        source_file=raw.source_file,
        source_row=raw.source_row,
        last_seen_date=raw.feed_date or as_of,
    )
    return member, issues
