-- Personal spending, in categories the account creates for itself.
--
-- Two tables rather than one. A category outlives any single expense — it is
-- created once and then either used or never used again — so folding it into
-- the expense row would mean either duplicating the name on every expense or
-- deleting a category by finding and rewriting every row that used it.
--
-- user_id is TEXT and namespaced exactly as in notes and reminders, so the
-- same accounting and pairing rules apply with no plugin-side special case.
--
-- Categories are per-account, not a shared dictionary. A shared one would mean
-- one account's categories appearing in another's analytics, which is the same
-- leak the notes table was designed to prevent. A guest on the demonstration
-- page gets its own set, and that is visible on the page as a feature.
--
-- is_archived rather than a hard delete: removing a category that still has
-- expenses must not take the expenses with it, and the monthly totals stay
-- comparable when a category is retired and a new one takes its place.

CREATE TABLE expense_categories (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    is_archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A name is unique among the active categories of one account. Reusing the
-- name of an archived one is allowed, since that is how a category is
-- genuinely replaced; the archive flag is what keeps the two apart.
--
-- The predicate is NOT is_archived, so the uniqueness applies to the rows the
-- account can see and pick from. Filtering on is_archived itself would cover
-- the opposite set and leave two live categories free to share a name, which
-- is exactly the case that makes a monthly report unreadable.
CREATE UNIQUE INDEX idx_expense_categories_active
    ON expense_categories (user_id, name) WHERE NOT is_archived;

-- The analytics read is always "this account, grouped by category", so both
-- halves of that query are indexed.
CREATE INDEX idx_expense_categories_user_id ON expense_categories (user_id);

-- NUMERIC, never FLOAT. Money in binary floating point does not add: the error
-- is small per row and it accumulates across every monthly total this table
-- exists to produce. The scale is fixed at two decimals, which is what makes
-- SUM() of a column exact rather than approximate.
--
-- spent_at is the day the money was actually spent, not the moment the row was
-- written. They differ whenever something is entered late, and a monthly
-- total that follows the entry date instead of the spending date is a report
-- that silently answers a different question.
CREATE TABLE expenses (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    category_id BIGINT NOT NULL REFERENCES expense_categories(id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL,
    spent_at DATE NOT NULL,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_expenses_user_id ON expenses (user_id);
-- The monthly breakdown groups by category over a date range; without both
-- halves of that predicate indexed the aggregation scans the whole table.
CREATE INDEX idx_expenses_category_date ON expenses (category_id, spent_at);

-- An amount of zero or less is rejected here rather than only in the plugin,
-- so a row written by any other path — a management command, a future import —
-- still cannot become a negative expense that quietly cancels a total.
ALTER TABLE expenses
    ADD CONSTRAINT expenses_amount_positive CHECK (amount > 0);