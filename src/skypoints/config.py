"""Static configuration: source mappings, country registry, domains.

Mirrors DIM_COUNTRY / DIM_COUNTRY_ALIAS in sql/02_ddl_targets.sql and the
per-source date formats in sql/03_load_staging.sql.
"""
from __future__ import annotations

STALE_DAYS = 90
MAX_AGE = 110
TIER_CODES = frozenset({"SLV", "GLD", "PLT"})
ACTIVE_FLAGS = frozenset({"A", "I"})

# Canonical column order for the raw record.
CANONICAL_COLUMNS = (
    "member_name", "member_id", "enrollment_date", "last_flight_date", "tier_code",
    "agent_name", "state", "country", "post_code", "dob", "is_active", "member_type",
)

# Columns the design spec says the GLOBAL feed must carry (11 fields).
SPEC_COLUMNS = (
    "member_name", "member_id", "enrollment_date", "last_flight_date", "tier_code",
    "agent_name", "state", "country", "post_code", "dob", "is_active",
)

# Date formats per source system. See normalize.parse_date.
#   MDY_SLASH   m/d/yyyy              (IND)
#   MDY_COMPACT mddyyyy, leading zero lost, left-pad to 8 digits (USA)
#   ISO         yyyy-mm-dd            (AUS)
#   YMD_COMPACT yyyymmdd              (GLOBAL pipe feed)
#   DMY_COMPACT ddmmyyyy, leading zero lost (GLOBAL DOB; ASSUMPTION, spec is ambiguous)
SOURCES: dict[str, dict] = {
    "IND": {
        "columns": {
            "ID": "member_id", "Name": "member_name", "DOB": "dob", "TierCode": "tier_code",
            "EnrollmentDate": "enrollment_date", "Individual or Corporate": "member_type",
            "Flight Date": "last_flight_date",
        },
        "date_formats": {"enrollment_date": "MDY_SLASH", "last_flight_date": "MDY_SLASH", "dob": "MDY_SLASH"},
        "fixed_country": "IND",
    },
    "USA": {
        "columns": {
            "ID": "member_id", "Name": "member_name", "TierCode": "tier_code",
            "EnrollmentDate": "enrollment_date", "FlightDate": "last_flight_date",
        },
        "date_formats": {"enrollment_date": "MDY_COMPACT", "last_flight_date": "MDY_COMPACT", "dob": "MDY_COMPACT"},
        "fixed_country": "USA",
    },
    "AUS": {
        "columns": {
            "Unique ID": "member_id", "Member Name": "member_name", "Tier Type": "tier_code",
            "Date of Birth": "dob", "Date of Enrollment": "enrollment_date", "Date of Flight": "last_flight_date",
        },
        "date_formats": {"enrollment_date": "ISO", "last_flight_date": "ISO", "dob": "ISO"},
        "fixed_country": "AUS",
    },
    "GLOBAL": {
        # pipe feed headers are already canonical (case-insensitive match)
        "columns": {
            "Member_Name": "member_name", "Member_Id": "member_id", "Enrollment_Date": "enrollment_date",
            "Last_Flight_Date": "last_flight_date", "Tier_Code": "tier_code", "Agent_Name": "agent_name",
            "State": "state", "Country": "country", "Post_Code": "post_code", "DOB": "dob",
            "Is_Active": "is_active",
        },
        "date_formats": {"enrollment_date": "YMD_COMPACT", "last_flight_date": "YMD_COMPACT", "dob": "DMY_COMPACT"},
        "fixed_country": None,
    },
}

# country_code -> target table (DIM_COUNTRY)
COUNTRIES: dict[str, str] = {
    "IND": "MEMBER_IND", "USA": "MEMBER_USA", "AUS": "MEMBER_AUS",
    "CAN": "MEMBER_CAN", "PHL": "MEMBER_PHL",
}

# alias -> country_code (DIM_COUNTRY_ALIAS)
COUNTRY_ALIASES: dict[str, str] = {
    "IND": "IND", "INDIA": "IND",
    "USA": "USA", "US": "USA",
    "AUS": "AUS", "AU": "AUS",
    "CAN": "CAN", "CA": "CAN",
    "PHL": "PHL", "PHIL": "PHL", "PH": "PHL",
}

# Feed mode per source system. 'delta': the file only lists changed members, so absence means
# nothing. 'snapshot': the file lists EVERY current member, so absence means the member has
# gone. Default is delta (never delete on a guess); switch a source to snapshot once the
# source team confirms it sends full extracts (CLI: --snapshot IND,USA).
FEED_MODE_DEFAULT = "delta"

# Per-source overrides of FEED_MODE_DEFAULT, e.g. {"IND": "snapshot"}. Seeded into DIM_SOURCE.
FEED_MODE: dict[str, str] = {}


def configured_snapshot_sources() -> set[str]:
    return {s for s in SOURCES if FEED_MODE.get(s, FEED_MODE_DEFAULT) == "snapshot"}


# Spec column lengths (design document). Over-length values are reported as FIELD_LENGTH
# warnings; the staging columns are wider than the spec so an INSERT never fails on them.
SPEC_LENGTHS = {"member_name": 255, "member_id": 18, "tier_code": 5, "state": 5, "agent_name": 255}
MAX_POST_CODE_DIGITS = 5

# Safety valve: a snapshot that lists fewer than (1 - this) of the currently ACTIVE members
# is treated as a truncated/partial file and is NOT used to mark anyone missing.
SNAPSHOT_DROP_GUARD = 0.20

# Attributes tracked in MEMBER_HISTORY (SCD Type 2). A change in any of them closes the
# current history row and opens a new one.
TRACKED_ATTRS = ("country_code", "tier_code", "is_active", "state")

# id namespace redemptions join to when the feed does not say (the partner feed carries none).
REDEMPTION_DEFAULT_SOURCE = "GLOBAL"

REDEMPTION_STATUSES = frozenset({"COMPLETED", "PENDING", "CANCELLED", "REVERSED"})
