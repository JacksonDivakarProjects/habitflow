-- Changes to logs already saved ("change yesterday's run to 6 km", "delete
-- Monday's reading"). Each request is confirmed by the user before it is
-- applied, and keeps the old values so it can be undone.

CREATE TABLE IF NOT EXISTS log_edits (
    edit_id      BIGSERIAL    PRIMARY KEY,
    chat_id      BIGINT       NOT NULL,
    request      TEXT         NOT NULL,
    action       VARCHAR(10)  NOT NULL,
    log_id       BIGINT       REFERENCES daily_logs (log_id),
    candidates   JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- log_ids to choose from
    changes      JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- requested: amount / unit / log_date
    old_values   JSONB,                                      -- the log before applying
    new_values   JSONB,                                      -- the log after applying
    status       VARCHAR(20)  NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_log_edits_action CHECK (action IN ('edit', 'delete')),
    CONSTRAINT chk_log_edits_status CHECK (
        status IN ('choosing', 'pending', 'applied', 'reverted', 'cancelled', 'superseded')
    )
);

CREATE INDEX IF NOT EXISTS idx_log_edits_chat_status ON log_edits (chat_id, status);
