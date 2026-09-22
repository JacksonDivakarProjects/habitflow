-- ============================================================
-- HabitFlow — transactional schema (OLTP)
-- Append-only fact tables. Facts captured at write time.
-- Analytics (medallion) built later as views, not columns.
-- ============================================================

CREATE TABLE habits (
    habit_id       SERIAL       PRIMARY KEY,
    name           VARCHAR(100) UNIQUE NOT NULL,
    display_name   TEXT         NOT NULL,
    description    TEXT,
    metric         VARCHAR(50),                 -- default unit; NULL if none is natural
    target_value   NUMERIC(10,2),
    target_metric  VARCHAR(50),
    is_active      BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_habits_target
        CHECK (target_value IS NULL OR target_value > 0),
    CONSTRAINT chk_habits_target_metric
        CHECK ((target_value IS NULL) = (target_metric IS NULL))
);

CREATE TABLE audit_log (
    audit_id         BIGSERIAL    PRIMARY KEY,
    chat_id          BIGINT       NOT NULL,
    message_id       BIGINT,

    user_input       TEXT         NOT NULL,
    received_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    intent           JSONB,
    draft_sql        TEXT,
    error_message    TEXT,
    user_feedback    TEXT,
    final_sql        TEXT,

    iteration_count  INTEGER      NOT NULL DEFAULT 0,
    status           VARCHAR(20)  NOT NULL DEFAULT 'pending',
    duration_ms      INTEGER,

    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_audit_status
        CHECK (status IN (
            'pending', 'awaiting_input',
            'approved', 'executed', 'failed', 'cancelled', 'superseded'
        ))
);

CREATE INDEX idx_audit_chat_status
    ON audit_log (chat_id, status);

CREATE INDEX idx_audit_created
    ON audit_log (created_at DESC);

CREATE UNIQUE INDEX uq_audit_pending_per_chat
    ON audit_log (chat_id)
    WHERE status = 'pending';

CREATE TABLE daily_logs (
    log_id           BIGSERIAL     PRIMARY KEY,
    habit_id         INTEGER       NOT NULL REFERENCES habits(habit_id),
    amount           NUMERIC(10,2) NOT NULL,
    metric           VARCHAR(50)   NOT NULL,     -- required per log
    logged_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    log_date         DATE          NOT NULL,
    source           VARCHAR(20)   NOT NULL DEFAULT 'manual',
    raw_input        TEXT,
    audit_id         BIGINT        REFERENCES audit_log(audit_id),
    metadata         JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_logs_amount
        CHECK (amount > 0),
    CONSTRAINT chk_logs_source
        CHECK (source IN ('manual', 'llm', 'import', 'api'))
);

CREATE INDEX idx_daily_logs_habit_date
    ON daily_logs (habit_id, log_date DESC);

CREATE INDEX idx_daily_logs_log_date
    ON daily_logs (log_date DESC);

CREATE INDEX idx_daily_logs_audit
    ON daily_logs (audit_id)
    WHERE audit_id IS NOT NULL;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_habits_updated_at
    BEFORE UPDATE ON habits
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_audit_log_updated_at
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();