CREATE INDEX idx_notes_chat_id ON notes (chat_id);
CREATE INDEX idx_reminders_chat_id ON reminders (chat_id);
CREATE INDEX idx_reminders_is_sent ON reminders (is_sent);
CREATE INDEX idx_clipboard_chat_id ON clipboard (chat_id);
