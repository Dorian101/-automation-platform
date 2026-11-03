-- When a session was last used, so the admin area can list the sign-ins that
-- are in use rather than every one that has not yet expired.
--
-- Written by the session lookup at most every few minutes, not on every
-- request. NULL means not used since this column appeared; readers fall back
-- to `created_at`.

ALTER TABLE sessions ADD COLUMN last_seen TIMESTAMP;
