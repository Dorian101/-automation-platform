-- Namespace user identity by transport.
--
-- Existing rows are Telegram chat ids, so they are prefixed before the column
-- type changes. Renaming first would leave legacy rows without a namespace and
-- they would stop matching any parsed identity.

ALTER TABLE notes ALTER COLUMN chat_id TYPE TEXT USING ('telegram:' || chat_id::TEXT);
ALTER TABLE reminders ALTER COLUMN chat_id TYPE TEXT USING ('telegram:' || chat_id::TEXT);
ALTER TABLE clipboard ALTER COLUMN chat_id TYPE TEXT USING ('telegram:' || chat_id::TEXT);

ALTER TABLE notes RENAME COLUMN chat_id TO user_id;
ALTER TABLE reminders RENAME COLUMN chat_id TO user_id;
ALTER TABLE clipboard RENAME COLUMN chat_id TO user_id;

-- Recreated under names that match the column they cover.
DROP INDEX IF EXISTS idx_notes_chat_id;
DROP INDEX IF EXISTS idx_reminders_chat_id;
DROP INDEX IF EXISTS idx_clipboard_chat_id;

CREATE INDEX idx_notes_user_id ON notes (user_id);
CREATE INDEX idx_reminders_user_id ON reminders (user_id);
CREATE INDEX idx_clipboard_user_id ON clipboard (user_id);
