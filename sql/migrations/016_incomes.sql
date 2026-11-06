-- Money coming in, beside the money going out.
--
-- A table of its own rather than negative expenses: `expenses` refuses an
-- amount of zero or less, and every total on the page is a plain SUM over it.
--
-- `kind` instead of categories. The month ends with two results — salary less
-- spending, and everything received less spending — so the only thing an
-- income has to say is whether it is salary. Who sent the rest goes in `note`.
--
-- The same NUMERIC(12, 2) and date-of-the-money rules as `expenses`.

CREATE TABLE incomes (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('salary', 'other')),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    received_at DATE NOT NULL,
    note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_incomes_user_date ON incomes (user_id, received_at);
