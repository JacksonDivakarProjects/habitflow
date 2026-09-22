"""
Structural check for LLM-drafted SQL, run before the EXPLAIN dry run.

draft_sql is never executed (/execute builds its own INSERT), but it is
EXPLAINed against the live database, so only one shape is accepted:
a single INSERT INTO daily_logs that reads from nothing but `habits`.
"""

import re
from typing import Optional

import sqlglot
from sqlglot import exp

TARGET_TABLE = "daily_logs"
READABLE_TABLES = {"habits"}

# Nodes that write, run arbitrary code, or can't be reasoned about.
FORBIDDEN_NODES = (
    exp.Delete,
    exp.Update,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
    exp.OnConflict,
    exp.With,
    exp.Anonymous,  # unknown functions, e.g. pg_sleep, dblink, pg_read_file
)

# `:name` bind parameters, but not the `::type` cast operator.
_PLACEHOLDER = re.compile(r"(?<!:):[a-zA-Z_][a-zA-Z0-9_]*")


def bind_nulls(sql: str) -> str:
    """Replace :named placeholders with NULL so the SQL can be EXPLAINed."""
    return _PLACEHOLDER.sub("NULL", sql)


def check_draft_sql(sql: str) -> Optional[str]:
    """Return None if the SQL is an acceptable draft, else the reason it isn't."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.SqlglotError as e:
        return f"could not parse SQL: {str(e).splitlines()[0]}"

    if len(statements) != 1:
        return f"expected exactly one statement, got {len(statements)}"
    stmt = statements[0]
    if not isinstance(stmt, exp.Insert):
        return f"expected an INSERT, got {stmt.key.upper()}"

    target = stmt.this.find(exp.Table)
    if target is None or target.name != TARGET_TABLE or target.db not in ("", "public"):
        return f"must INSERT INTO {TARGET_TABLE}"

    for node in stmt.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return f"{node.key.upper()} is not allowed"
        if isinstance(node, exp.Insert) and node is not stmt:
            return "nested INSERT is not allowed"
        if isinstance(node, exp.Table) and node is not target:
            if node.name not in READABLE_TABLES:
                return f"may only read from {', '.join(sorted(READABLE_TABLES))}, not {node.name}"

    return None
