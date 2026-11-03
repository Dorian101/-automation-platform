-- What the administration area can change and read without SSH.
--
-- `settings` holds switches set from /admin/settings: a plugin turned off,
-- maintenance, open registration, the limits on the public forms. A key that
-- is absent means "the default", which is what `.env` says or the code does,
-- so an empty table behaves exactly as before.
--
-- `app_errors` keeps the application's own ERROR log records for
-- /admin/errors. The service user cannot read the journal, deliberately, so
-- the application writes its errors where it can show them. Capped by the
-- writer at the newest few hundred rows.
--
-- `sessions` gains an `id` to name a row in a form without exposing the token
-- digest, and `agent`, the device label of the browser that signed in.

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE app_errors (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

ALTER TABLE sessions
    ADD COLUMN id BIGSERIAL UNIQUE,
    ADD COLUMN agent TEXT;
