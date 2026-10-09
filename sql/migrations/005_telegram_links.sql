-- A link between a web account and a Telegram chat.
--
-- This is deliberately a separate table rather than a column on users: that
-- table is about web credentials, and a column here would make every account
-- carry a notification route it does not necessarily have. A link is not an
-- account — it is a statement that two identities belong to the same person.
--
-- Neither side identifies a user on its own. Web data stays under web:<name>
-- and Telegram data stays under telegram:<id>; the link only decides whether
-- the two are read together.
--
-- UNIQUE on telegram_id enforces one-to-one. A Telegram chat cannot belong to
-- two web accounts, which is what stops a shared login from silently merging
-- two people's data. ON DELETE CASCADE removes the link when the account goes,
-- so a deleted user cannot leave a chat addressable.

CREATE TABLE telegram_links (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    telegram_id BIGINT NOT NULL UNIQUE,
    linked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);