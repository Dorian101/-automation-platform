ALTER TABLE share_files ADD COLUMN user_id TEXT;

UPDATE share_files sf SET user_id = s.user_id FROM shares s WHERE s.id = sf.share_id;

ALTER TABLE share_files ALTER COLUMN user_id SET NOT NULL;

CREATE INDEX idx_share_files_user ON share_files (user_id);
