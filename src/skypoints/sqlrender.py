"""Render and split the SQL files.

The SQL under sql/ uses ``${NAME}`` template variables (so one file serves every load and
every country table) and plain ``;`` between statements. Snowflake's Python connector runs
one statement per call, so files are split here. The splitter understands comments, string
literals and ``$$ ... $$`` function bodies, so a ``;`` inside any of them is not a boundary.
"""
from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Every variable the runner can supply. A file using any other name is a bug (tested).
KNOWN_VARS = frozenset({
    "LOAD_ID", "AS_OF", "STALE_DAYS", "COUNTRY_CODE", "TARGET_TABLE", "SOURCE_SYSTEM", "FEED_DATE",
    "FROM_DATE", "REFRESH_ALL", "BIRTHDAY_FILTER", "FLIP_FROM", "FLIP_TO", "RETENTION_DAYS",
})


def template_vars(sql: str) -> set[str]:
    return set(_VAR.findall(sql))


def render(sql: str, variables: dict[str, str]) -> str:
    """Substitute ``${NAME}``; a missing variable is an error, never an empty string.
    Snowflake's own ``$1`` / ``METADATA$FILENAME`` are untouched (only ``${`` is special)."""
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in variables:
            raise KeyError(f"SQL template variable ${{{name}}} was not supplied")
        return str(variables[name])
    return _VAR.sub(sub, sql)


def split_statements(sql: str) -> list[str]:
    """Split on ';' outside comments, strings and $$ blocks. Comments are dropped."""
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        two = sql[i:i + 2]
        if two == "--":                                   # line comment
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":                                   # block comment
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if two == "$$":                                   # function body: copy verbatim
            j = sql.find("$$", i + 2)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        if c == "'":                                      # string literal ('' is an escaped quote)
            j = i + 1
            while j < n:
                if sql[j] == "'" and sql[j + 1:j + 2] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def load_sql(name: str, sql_dir: Path | None = None) -> str:
    return (Path(sql_dir) if sql_dir else SQL_DIR).joinpath(name).read_text(encoding="utf-8")


def statements(name: str, variables: dict[str, str] | None = None, sql_dir: Path | None = None) -> list[str]:
    return split_statements(render(load_sql(name, sql_dir), variables or {}))
