CREATE TABLE files (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    size BIGINT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    stored TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP + INTERVAL '1 day'
);

CREATE INDEX idx_files_user ON files (user_id);

CREATE TABLE shares (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_shares_user ON shares (user_id);

CREATE TABLE share_files (
    share_id BIGINT NOT NULL REFERENCES shares(id) ON DELETE CASCADE,
    file_id BIGINT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY (share_id, file_id)
);

CREATE INDEX idx_share_files_file ON share_files (file_id);
