-- Feedback left on the public pages: a line of text and the page it came from.
--
-- Anonymous on purpose. No identity, no address, no contact: the form asks for
-- the text only, so the table holds the text only. `page` is one of the public
-- pages or empty, never a free string from the request.
--
-- Kept even when the Telegram message to the owner goes out, so a message that
-- failed to send is still here to read.

CREATE TABLE feedback (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    page TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL
);
