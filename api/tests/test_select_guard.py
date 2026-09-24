"""The guard for questions' SQL: read-only, habit data only, bounded."""

import pytest

from app.sqlguard import MAX_QUERY_ROWS, check_select_sql

LIMIT = MAX_QUERY_ROWS + 1


@pytest.mark.parametrize("sql", [
    "SELECT sum(amount_in_habit_unit) FROM habit_logs WHERE habit = 'reading'",
    "SELECT weekday_name, round(avg(amount), 1) AS avg FROM habit_logs "
    "WHERE habit = 'reading' GROUP BY weekday, weekday_name ORDER BY weekday",
    "SELECT week_start, sum(coalesce(amount_in_habit_unit, amount)) FROM habit_logs "
    "WHERE log_date >= DATE '2026-09-01' GROUP BY 1 ORDER BY 1",
    "WITH d AS (SELECT DISTINCT log_date FROM habit_logs WHERE habit = 'running') "
    "SELECT count(*) FROM d",
    "SELECT h.display_name, count(l.log_id) FROM habits h "
    "LEFT JOIN habit_logs l ON l.habit = h.name GROUP BY 1",
    "SELECT d::date AS day, coalesce(sum(l.amount), 0) FROM "
    "generate_series(DATE '2026-09-01', DATE '2026-09-07', INTERVAL '1 day') AS d "
    "LEFT JOIN habit_logs l ON l.log_date = d::date GROUP BY 1 ORDER BY 1",
    "SELECT log_date, amount, lag(amount) OVER (ORDER BY log_date) FROM habit_logs",
    "SELECT habit, rank() OVER (ORDER BY sum(amount) DESC) FROM habit_logs GROUP BY habit",
    "SELECT to_char(log_date, 'Mon YYYY'), string_agg(DISTINCT habit, ', ') "
    "FROM habit_logs GROUP BY 1",
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) FROM habit_logs",
    "SELECT date_trunc('month', log_date), extract(dow FROM log_date) FROM habit_logs",
    "SELECT CASE WHEN amount > 5 THEN 'long' ELSE 'short' END, count(*) FROM habit_logs GROUP BY 1",
    "SELECT habit FROM habit_logs UNION SELECT name FROM habits",
    "SELECT max(log_date) - min(log_date) FROM public.habit_logs",
    "SELECT '100%' AS pct, amount FROM habit_logs WHERE habit LIKE 'read%'",
    "SELECT 1 AS n;",  # trailing semicolon is fine
])
def test_allowed(sql):
    error, runnable = check_select_sql(sql)
    assert error is None, error
    assert f"LIMIT {LIMIT}" in runnable or "LIMIT" in runnable


@pytest.mark.parametrize("sql,why", [
    ("", "empty"),
    ("   ", "empty"),
    ("DELETE FROM daily_logs", "only SELECT"),
    ("UPDATE habits SET name = 'x'", "only SELECT"),
    ("INSERT INTO daily_logs (habit_id) VALUES (1)", "only SELECT"),
    ("DROP TABLE habits", "only SELECT"),
    ("TRUNCATE daily_logs", "only SELECT"),
    ("SELECT 1; DELETE FROM daily_logs", "exactly one statement"),
    ("SELECT 1; SELECT 2", "exactly one statement"),
    ("WITH gone AS (DELETE FROM daily_logs RETURNING *) SELECT * FROM gone", "DELETE"),
    ("SELECT * INTO copy_of_logs FROM habit_logs", "INTO"),
    ("SELECT * FROM habit_logs FOR UPDATE", "LOCK"),
    ("SELECT * FROM daily_logs", "not daily_logs"),       # bypasses voided_at filter
    ("SELECT * FROM audit_log", "not audit_log"),
    ("SELECT * FROM query_log", "not query_log"),
    ("SELECT * FROM pg_catalog.pg_user", "pg_catalog"),
    ("SELECT * FROM pg_shadow", "not pg_shadow"),
    ("SELECT * FROM information_schema.tables", "information_schema"),
    ("SELECT * FROM other_db.public.habits", "other_db"),
    ("SELECT * FROM habit_logs h JOIN daily_logs d ON d.log_id = h.log_id", "daily_logs"),
    ("SELECT (SELECT count(*) FROM audit_log) FROM habit_logs", "audit_log"),
    ("SELECT version()", "not allowed"),
    ("SELECT current_user", "not allowed"),
    ("SELECT pg_sleep(10)", "pg_sleep"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
    ("SELECT set_config('statement_timeout', '0', false)", "set_config"),
    ("SELECT current_setting('server_version')", "current_setting"),
    ("SELECT lo_import('/etc/passwd')", "lo_import"),
    ("SELECT dblink('host=evil', 'select 1')", "dblink"),
    ("SELECT sum(amount) FROM habit_logs WHERE log_date >= now() - interval '7 days'",
     "literal dates"),
    ("SELECT * FROM habit_logs WHERE log_date = CURRENT_DATE", "literal dates"),
    ("SELECT * FROM habit_logs WHERE logged_at > CURRENT_TIMESTAMP", "literal dates"),
    ("SELEC * FROM habit_logs", "parse"),
    ("COPY habits TO '/tmp/x'", "SELECT"),
])
def test_rejected(sql, why):
    error, runnable = check_select_sql(sql)
    assert error is not None, f"should be rejected: {sql}"
    assert why.lower() in error.lower(), error
    assert runnable == ""


def test_limit_is_added_when_missing():
    _, runnable = check_select_sql("SELECT * FROM habit_logs")
    assert runnable.endswith(f"LIMIT {LIMIT}")


def test_a_bigger_limit_is_capped():
    _, runnable = check_select_sql("SELECT * FROM habit_logs LIMIT 100000")
    assert runnable.endswith(f"LIMIT {LIMIT}") and "100000" not in runnable


def test_a_smaller_limit_is_kept():
    _, runnable = check_select_sql("SELECT * FROM habit_logs ORDER BY log_date DESC LIMIT 5")
    assert runnable.endswith("LIMIT 5")


def test_a_non_literal_limit_is_replaced():
    _, runnable = check_select_sql("SELECT * FROM habit_logs LIMIT (SELECT 999999)")
    assert runnable.endswith(f"LIMIT {LIMIT}")


def test_set_operations_are_limited_as_a_whole():
    _, runnable = check_select_sql("SELECT habit FROM habit_logs UNION ALL SELECT name FROM habits")
    assert runnable.startswith("SELECT * FROM (") and runnable.endswith(f"LIMIT {LIMIT}")


def test_cte_names_cannot_smuggle_other_tables():
    error, _ = check_select_sql(
        "WITH habit_logs AS (SELECT * FROM daily_logs) SELECT * FROM habit_logs"
    )
    assert error and "daily_logs" in error
