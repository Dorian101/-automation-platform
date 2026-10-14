-- Something that happened on the public side of the site, and when.
--
-- A log, not a table of people. There is no address here, no fingerprint and
-- no cookie of a stranger's: a reader who arrives with nothing is recorded
-- as having arrived and nothing more. A visitor on the demonstration has a
-- `web:guest:<token>`, which is a random string that resolves to no account
-- and stands for nothing once the guest namespaces are pruned.
--
-- The reason it holds no address is that an address would have told the owner
-- nothing they did not already have. A page view is worth knowing about for
-- when it happened, and correlating that with a résumé site is how the
-- question "who was that" gets answered — there is nothing in here to
-- correlate against, on purpose.
--
-- `kind` is a view of a public page, a sign-in, or a sign-up. Failed sign-ins
-- are not here: they are in the journal, with an address, where a security
-- signal belongs and where nobody reads it as a visit.
--
-- No index. The table is pruned on a schedule and read newest-first in one
-- range; at that size a sequential scan is the cheaper plan, and an index
-- would be paid for on every insert — the one operation that happens on every
-- request, which is the whole point of the table.

CREATE TABLE visits (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    kind TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    identity TEXT
);
