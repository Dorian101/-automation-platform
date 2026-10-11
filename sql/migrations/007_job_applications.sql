-- Job applications, tracked per account.
--
-- user_id is TEXT and namespaced exactly as in notes and expenses, so the
-- same isolation and pairing rules apply with no plugin-side special case.
--
-- status is the machine's current stage, stored as a key rather than a
-- label: the label is what a reader sees and it will be translated or
-- reworded, while the key is what the transitions are written against. The
-- CHECK is the same bargain as the positive amount on expenses — the rule
-- belongs to the database, so a row written by any other path still cannot
-- hold a stage that does not exist.
--
-- applied_at is when the application was sent, and is what the silence count
-- is measured from. created_at is when the row was written; the two differ
-- whenever something is entered late, and "days since" a question about the
-- application, not about the bookkeeping.

CREATE TABLE job_applications (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL DEFAULT 'applied',
    applied_at DATE NOT NULL,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The stage list, in the order it is stored and reported.
ALTER TABLE job_applications
    ADD CONSTRAINT job_applications_status_known
    CHECK (status IN ('applied', 'answered', 'interview', 'offer', 'rejected'));

-- The list is read as "this account's applications, newest first", which is
-- the ordering the page shows and the funnel counts.
CREATE INDEX idx_job_applications_user_id ON job_applications (user_id, applied_at DESC);

-- The funnel is a group-by on the status of one account; without this the
-- aggregation scans every row this account has ever written.
CREATE INDEX idx_job_applications_status ON job_applications (user_id, status);
