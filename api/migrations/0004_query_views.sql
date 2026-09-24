-- Questions ("how much did I read this month?") are answered with SELECTs over
-- the habit_logs view below: undone (voided) logs are already excluded, and
-- amounts are converted to each habit's own unit where the units are
-- compatible, so totals over mixed units (5 km + 1 mile) come out right.

CREATE TABLE IF NOT EXISTS unit_conversions (
    unit     TEXT    PRIMARY KEY,
    family   TEXT    NOT NULL,
    to_base  NUMERIC NOT NULL          -- factor to the family's base unit
);

INSERT INTO unit_conversions (unit, family, to_base) VALUES
    ('km',      'distance', 1),
    ('miles',   'distance', 1.609344),
    ('meters',  'distance', 0.001),
    ('minutes', 'time',     1),
    ('hours',   'time',     60),
    ('seconds', 'time',     0.0166666666666667),
    ('liters',  'volume',   1),
    ('ml',      'volume',   0.001)
ON CONFLICT (unit) DO UPDATE SET family = EXCLUDED.family, to_base = EXCLUDED.to_base;

CREATE OR REPLACE VIEW habit_logs AS
SELECT
    dl.log_id,
    h.name                                     AS habit,          -- e.g. 'reading'
    h.display_name                             AS habit_display,  -- e.g. 'Reading'
    dl.amount,
    r.unit                                     AS unit,           -- unit as logged
    h.metric                                   AS habit_unit,     -- the habit's default unit
    CASE
        WHEN r.unit = h.metric THEN dl.amount
        WHEN f.family IS NOT NULL AND f.family = t.family
            THEN round(dl.amount * f.to_base / t.to_base, 4)
    END                                        AS amount_in_habit_unit,  -- NULL if not convertible
    dl.log_date,
    dl.logged_at,
    EXTRACT(ISODOW FROM dl.log_date)::int      AS weekday,        -- 1 = Monday .. 7 = Sunday
    trim(to_char(dl.log_date, 'Day'))          AS weekday_name,   -- 'Monday'
    date_trunc('week', dl.log_date)::date      AS week_start,     -- Monday of that week
    date_trunc('month', dl.log_date)::date     AS month_start
FROM daily_logs dl
JOIN habits h ON h.habit_id = dl.habit_id
LEFT JOIN unit_conversions t ON t.unit = h.metric
-- a bare "m" (old rows) means meters for distance habits, minutes for time habits
CROSS JOIN LATERAL (
    SELECT CASE
        WHEN dl.metric = 'm' AND t.family = 'distance' THEN 'meters'
        WHEN dl.metric = 'm' AND t.family = 'time' THEN 'minutes'
        ELSE dl.metric
    END AS unit
) r
LEFT JOIN unit_conversions f ON f.unit = r.unit
WHERE dl.voided_at IS NULL;

-- Every question asked, with the SQL used, for accuracy checks and the
-- "Show SQL" button.
CREATE TABLE IF NOT EXISTS query_log (
    query_id     BIGSERIAL    PRIMARY KEY,
    chat_id      BIGINT       NOT NULL,
    question     TEXT         NOT NULL,
    sql          TEXT,
    source       VARCHAR(20)  NOT NULL,     -- 'template' | 'llm' | 'none'
    row_count    INTEGER,
    answer       TEXT,
    error        TEXT,
    attempts     INTEGER      NOT NULL DEFAULT 1,
    duration_ms  INTEGER,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_query_log_created ON query_log (created_at DESC);
