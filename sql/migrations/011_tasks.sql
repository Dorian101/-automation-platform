-- Tasks, web-only: a person's to-do pile with status, priority and a deadline.
--
-- A task is a small thing held until it is done: a line of text, a status in a
-- fixed set of three (todo, doing, done), an optional deadline and an optional
-- priority. `created_at` is set by the database and never edited by hand — a
-- task's creation date is a fact, not a field to curate.
--
-- `priority` is 1, 2 or 3, one being the highest; NULL means an ordinary task
-- that has not been prioritised. `due_at` is NULL when there is no deadline.
-- Both are deliberately optional: the pile is where tasks go to not be
-- forgotten, and a task with neither is still a task, not a broken row.
--
-- `notified_at` records when the owner was last told the deadline is near, so a
-- worker can remind once per deadline rather than on every pass. NULL means
-- never reminded.
--
-- No index beyond the sequence: this table is read per person and small, and
-- the same reasoning as the visits table applies — an index would be paid on
-- every insert, the one operation a growing pile does all day.

CREATE TABLE tasks (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    priority INTEGER,
    due_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notified_at TIMESTAMP
);