-- What tells one visitor from another, without keeping who they are.
--
-- `visitor` is a short hash of the address and the browser string under a salt
-- that changes every day and is then deleted. Two visits on one day from one
-- browser share it; the same person tomorrow gets another, and with the salt
-- gone nobody can work back to the address or follow them across days.
--
-- `agent` is a short description of the browser ("iPhone · Safari"), `bot`
-- marks crawlers and link previews, `campaign` is the `?from=` tag of a link
-- the owner handed out. All nullable: rows written before this carry none.
--
-- `visit_salts` holds today's salt only; older ones are deleted when a new
-- day's is created.

ALTER TABLE visits
    ADD COLUMN visitor TEXT,
    ADD COLUMN agent TEXT,
    ADD COLUMN bot BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN campaign TEXT;

CREATE TABLE visit_salts (
    day DATE PRIMARY KEY,
    salt BYTEA NOT NULL
);
