-- Undo: a voided log is kept (daily_logs is append-only) but excluded
-- from stats, streaks and "today".
ALTER TABLE daily_logs ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_daily_logs_live
    ON daily_logs (habit_id, log_date DESC)
    WHERE voided_at IS NULL;
