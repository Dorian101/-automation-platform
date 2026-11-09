ALTER TABLE reminders
    ADD COLUMN repeat TEXT CHECK (repeat IN ('daily', 'weekdays', 'weekly'));

ALTER TABLE users
    ADD COLUMN quiet_from TIME,
    ADD COLUMN quiet_to TIME;

CREATE TABLE held_notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    body JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
);

CREATE INDEX idx_held_notifications_user ON held_notifications (user_id);
