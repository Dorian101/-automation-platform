CREATE TABLE bookmarks (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT NOT NULL CHECK (source IN ('web', 'telegram')),
    saved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, url)
);

CREATE INDEX idx_bookmarks_user ON bookmarks (user_id, saved_at);
