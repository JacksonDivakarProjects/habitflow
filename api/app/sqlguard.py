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


# ------------------------------------------------------------------
# Questions: read-only SELECTs over the habit_logs view
# ------------------------------------------------------------------
QUERY_RELATIONS = {"habit_logs", "habits"}
MAX_QUERY_ROWS = 200

# Functions a question may use. Anything else (version(), pg_sleep, set_config,
# current_user, dblink, lo_import, ...) is rejected: a whitelist can't be
# bypassed by a function we didn't think of.
_ALLOWED_FUNCS = tuple(
    getattr(exp, name) for name in (
        "Sum", "Avg", "Count", "Min", "Max", "Stddev", "StddevPop", "StddevSamp",
        "Variance", "VariancePop", "PercentileCont", "PercentileDisc", "GroupConcat",
        "ArrayAgg", "Round", "Abs", "Ceil", "Floor", "Sqrt", "Pow", "Mod", "Coalesce",
        "Nullif", "Greatest", "Least", "Case", "If", "Cast", "TryCast", "Extract",
        "TimestampTrunc", "DateTrunc", "TimeToStr", "StrToDate", "DateAdd", "DateSub",
        "DateDiff", "Lower", "Upper", "Trim", "Length", "Concat", "Lag", "Lead", "Rank",
        "DenseRank", "RowNumber", "PercentRank", "CumeDist", "FirstValue", "LastValue",
        "NthValue", "Ntile", "GenerateSeries", "ExplodingGenerateSeries",
    ) if hasattr(exp, name)
)
_ALLOWED_ANONYMOUS = {"age", "date_part", "to_date", "trunc", "mode", "justify_days"}
_DATE_NOW = tuple(getattr(exp, n) for n in (
    "CurrentDate", "CurrentTimestamp", "CurrentTime", "Localtimestamp", "Localtime",
) if hasattr(exp, n))
_QUERY_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Command, exp.Copy, exp.Into, exp.Lock,
)
_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
# sqlglot models some operators as functions too (AND, OR, ~, @>, ^): values only.
_OPERATORS = (exp.Binary, exp.Unary, exp.Connector, exp.Predicate)


def check_select_sql(sql: str, max_rows: int = MAX_QUERY_ROWS) -> tuple[Optional[str], str]:
    """Validate a question's SQL. Returns (error, runnable_sql): error is None
    when the SQL is acceptable, and runnable_sql then has a row limit applied
    (at most max_rows + 1, so the caller can tell the result was truncated)."""
    if not sql or not sql.strip():
        return "the SQL is empty", ""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.SqlglotError as e:
        return f"could not parse SQL: {str(e).splitlines()[0]}", ""
    if len(statements) != 1:
        return f"expected exactly one statement, got {len(statements)}", ""
    root = statements[0]
    if not isinstance(root, _ROOTS):
        return f"only SELECT queries are allowed, got {root.key.upper()}", ""

    cte_names = {cte.alias_or_name for cte in root.find_all(exp.CTE)}
    readable = ", ".join(sorted(QUERY_RELATIONS))
    for node in root.walk():
        if isinstance(node, _QUERY_FORBIDDEN):
            return f"{node.key.upper()} is not allowed in a question", ""
        if isinstance(node, _DATE_NOW):
            return ("don't use CURRENT_DATE/NOW(): use the literal dates given in DATES "
                    "(e.g. log_date >= '2026-09-01')"), ""
        if isinstance(node, exp.Anonymous):
            if node.name.lower() not in _ALLOWED_ANONYMOUS:
                return f"function {node.name}() is not allowed", ""
        elif (isinstance(node, exp.Func) and not isinstance(node, _OPERATORS)
              and not isinstance(node, _ALLOWED_FUNCS)):
            return f"function {node.key}() is not allowed", ""
        if isinstance(node, exp.Table):
            if isinstance(node.this, exp.Func):  # generate_series(...) AS d
                continue
            if node.db not in ("", "public") or node.catalog:
                return f"only {readable} can be queried, not {node.sql()}", ""
            if node.name not in QUERY_RELATIONS and node.name not in cte_names:
                return (f"only {readable} can be queried "
                        f"(undone logs are already excluded there), not {node.name}"), ""

    limit = max_rows + 1
    if isinstance(root, exp.Select):
        existing = root.args.get("limit")
        current = None
        if existing is not None and isinstance(existing.expression, exp.Literal):
            try:
                current = int(existing.expression.this)
            except ValueError:
                current = None
        if current is None or current > limit:
            root = root.limit(limit)
        runnable = root.sql(dialect="postgres")
    else:  # UNION / INTERSECT / EXCEPT: limit the combined result
        runnable = f"SELECT * FROM ({root.sql(dialect='postgres')}) AS q LIMIT {limit}"
    return None, runnable
