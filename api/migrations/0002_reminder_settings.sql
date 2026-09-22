-- Evening check-in reminders, one row per chat (set with /remind in the bot).
CREATE TABLE IF NOT EXISTS reminder_settings (
    chat_id     BIGINT      PRIMARY KEY,
    remind_at   TIME        NOT NULL,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
