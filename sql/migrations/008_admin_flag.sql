-- Whether an account may reach the administrative area.
--
-- A boolean and not a roles table, because there is exactly one kind of
-- question being asked: may this person open the pages that act on the
-- platform rather than on their own data. A roles table would be a general
-- mechanism with a single row in it, and the first person to need a second
-- role could add the table then — with the thing it is for already written
-- down.
--
-- The default is false, for the same reason the `demo` flag defaults to
-- false: an account that nobody decided anything about does not get to act
-- on everyone else's data. The flag is granted from the host, one account at
-- a time, by someone who has the shell.
--
-- Not an index. The column is read on the way into the administrative area
-- and written when a flag is granted, which is a handful of times in the life
-- of a deployment. An index on it would cost on every insert and pay for
-- nothing.

ALTER TABLE users ADD COLUMN is_admin boolean NOT NULL DEFAULT false;
