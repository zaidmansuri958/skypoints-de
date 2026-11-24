"""Reference data for Snowflake, generated from config.py.

config.py is the ONE definition of countries, aliases, per-source date formats and allowed
values. The SQL never hard-codes them: the runner seeds DIM_COUNTRY, DIM_COUNTRY_ALIAS,
DIM_SOURCE and DIM_DOMAIN from here, and generates the per-country tables and the
MEMBER_ALL view. Changing a date format or adding a country is therefore one edit that both
the Python reference and the Snowflake load pick up.
"""
from __future__ import annotations

from .config import (ACTIVE_FLAGS, COUNTRIES, COUNTRY_ALIASES, FEED_MODE, FEED_MODE_DEFAULT,
                     REDEMPTION_STATUSES, SOURCES, TIER_CODES)


def _q(value: object) -> str:
    """SQL literal: NULL, or a single-quoted string with quotes doubled."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _insert(table: str, columns: list[str], rows: list[tuple]) -> str:
    values = ",\n  ".join("(" + ", ".join(_q(v) for v in r) + ")" for r in rows)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n  {values}"


def seed_statements() -> list[str]:
    """DELETE + INSERT pairs that make the DIM tables equal to config.py."""
    countries = sorted(COUNTRIES.items())
    aliases = sorted(COUNTRY_ALIASES.items())
    sources = [(name, cfg["fixed_country"], cfg["date_formats"]["enrollment_date"],
                cfg["date_formats"]["last_flight_date"], cfg["date_formats"]["dob"],
                FEED_MODE.get(name, FEED_MODE_DEFAULT))
               for name, cfg in sorted(SOURCES.items())]
    domains = ([("TIER", v) for v in sorted(TIER_CODES)]
               + [("ACTIVE", v) for v in sorted(ACTIVE_FLAGS)]
               + [("REDEMPTION_STATUS", v) for v in sorted(REDEMPTION_STATUSES)])
    return [
        "DELETE FROM DIM_COUNTRY",
        _insert("DIM_COUNTRY", ["country_code", "target_table"], countries),
        "DELETE FROM DIM_COUNTRY_ALIAS",
        _insert("DIM_COUNTRY_ALIAS", ["alias", "country_code"], aliases),
        "DELETE FROM DIM_SOURCE",
        _insert("DIM_SOURCE", ["source_system", "fixed_country", "enroll_fmt", "flight_fmt", "dob_fmt",
                               "feed_mode"], sources),
        "DELETE FROM DIM_DOMAIN",
        _insert("DIM_DOMAIN", ["domain", "value"], domains),
    ]


def country_table_statements() -> list[str]:
    """One table per configured country, shaped exactly like the MEMBER_IND template."""
    return [f"CREATE TABLE IF NOT EXISTS {table} LIKE MEMBER_IND"
            for _, table in sorted(COUNTRIES.items()) if table != "MEMBER_IND"]


def member_all_view_sql() -> str:
    """MEMBER_ALL = UNION ALL of every country table, generated from the registry so it can
    never drift from DIM_COUNTRY. table_name lets the reconcile checks catch a row sitting
    in the wrong country table."""
    branches = [f"SELECT '{table}' AS table_name, t.* FROM {table} t" for _, table in sorted(COUNTRIES.items())]
    return "CREATE OR REPLACE VIEW MEMBER_ALL AS\n" + "\nUNION ALL\n".join(branches)
