-- A task's details, when it was finished, and whether it comes back.
--
-- `description` holds what one line of text cannot.
--
-- `done_at` is when the task last reached `done`, so the board shows a week of
-- finished work and the rest goes to the archive. Unknown for tasks finished
-- before this column existed; their creation time stands in.
--
-- `repeat` makes finishing a task create the next one, and `repeated` records
-- that it did, so moving a task out of `done` and back cannot make a second.

ALTER TABLE tasks
    ADD COLUMN description TEXT,
    ADD COLUMN done_at TIMESTAMP,
    ADD COLUMN repeat TEXT CHECK (repeat IN ('daily', 'weekly', 'monthly')),
    ADD COLUMN repeated BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE tasks SET done_at = created_at WHERE status = 'done';
