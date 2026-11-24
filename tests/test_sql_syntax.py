"""Every statement in sql/ must parse with a Snowflake-dialect parser.

This is a syntax check only: it proves nothing about behaviour (that is what the Snowflake
`demo` parity run is for). The security DDL (roles, masking and row access policies) is
Snowflake-specific syntax the parser does not know, so those two files are excluded rather
than silently half-checked.
"""
import logging

import pytest

sqlglot = pytest.importorskip("sqlglot")

from skypoints.reference import country_table_statements, member_all_view_sql, seed_statements
from skypoints.sqlrender import KNOWN_VARS, SQL_DIR, statements

UNPARSEABLE = {"14_security.sql", "14b_security_country.sql"}
VARS = {k: "X" for k in KNOWN_VARS} | {
    "AS_OF": "2026-11-21", "FROM_DATE": "2026-11-20", "FLIP_FROM": "2026-08-22", "FLIP_TO": "2026-08-23",
    "REFRESH_ALL": "FALSE", "BIRTHDAY_FILTER": "TO_CHAR(t.dob,'MMDD') IN ('1121')", "STALE_DAYS": "90",
    "LOAD_ID": "L1", "TARGET_TABLE": "MEMBER_IND", "COUNTRY_CODE": "IND", "SOURCE_SYSTEM": "GLOBAL",
    "FEED_DATE": "2024-01-15", "RETENTION_DAYS": "30"}


def parse(stmt):
    return sqlglot.parse_one(stmt, read="snowflake", error_level=sqlglot.ErrorLevel.RAISE)


@pytest.mark.parametrize("name", sorted(p.name for p in SQL_DIR.glob("*.sql") if p.name not in UNPARSEABLE))
def test_sql_file_parses_as_snowflake(name, caplog):
    with caplog.at_level(logging.WARNING, logger="sqlglot"):
        for stmt in statements(name, VARS):
            parse(stmt)
    # a "Falling back to parsing as a 'Command'" warning means the parser did NOT really check it
    assert not [r for r in caplog.records if "Command" in r.getMessage()], name


def test_generated_sql_parses():
    for stmt in seed_statements() + country_table_statements() + [member_all_view_sql()]:
        parse(stmt)
