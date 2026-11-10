"""Storage for tasks, the web-only to-do pile.

Follows the notes repository's shape: identity-scoped writes, reads widened
across a linked pair, no shared namespace. A task belongs to the identity that
wrote it and a read may span the pair, the same rule every personal plugin
already uses.
"""

from collections.abc import Iterable
from datetime import datetime

from app.core.identity import Identity, as_tuple, ids_of

from .database import Database

# The fixed statuses. `todo` is the default and the pile; `doing` and `done` are
# the two ways a task stops being a bare reminder of itself.
STATUSES = ("todo", "doing", "done")

# `priority` is 1, 2 or 3, one being the highest. Any other integer is noise
# that would sort unpredictably, so the repository is not asked to store it.
VALID_PRIORITIES = {1, 2, 3}

# How often a repeating task comes back once it is done.
REPEATS = ("daily", "weekly", "monthly")


class TasksRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(
        self,
        identity: Identity,
        text: str,
        priority: int | None = None,
        due_at: datetime | None = None,
        status: str = "todo",
        description: str | None = None,
        repeat: str | None = None,
        done_at: datetime | None = None,
    ) -> None:
        """Write one task. A task written as done is done now unless told when."""
        if status == "done" and done_at is None:
            done_at = datetime.now()

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tasks (user_id, text, priority, due_at, status,
                                       description, repeat, done_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(identity),
                        text,
                        priority,
                        due_at,
                        status,
                        description,
                        repeat,
                        done_at,
                    ),
                )
            conn.commit()

    def entries(
        self,
        identity: Identity | Iterable[Identity],
        status: str | None = None,
        order: str = "created",
    ) -> list[dict]:
        """Return every task this read can see, oldest last.

        `order` picks the sort: ``created``, ``priority``, ``due``. ``status``
        narrows to one of the three; None returns all.
        """
        identities = as_tuple(identity)

        where = ""
        arguments: list = [ids_of(identities)]

        if status is not None:
            where = " AND status = %s"
            arguments.append(status)

        order_by = {
            "created": "created_at DESC, id DESC",
            "priority": "priority ASC NULLS LAST, created_at DESC, id DESC",
            "due": "due_at ASC NULLS LAST, priority ASC NULLS LAST, id DESC",
        }[order]

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, text, status, priority, due_at, created_at,
                           description, done_at, repeat, repeated
                    FROM tasks
                    WHERE user_id = ANY(%s){where}
                    ORDER BY {order_by}
                    """,
                    arguments,
                )
                rows = cur.fetchall()

        return [
            {
                "id": row[0],
                "text": row[1],
                "status": row[2],
                "priority": row[3],
                "due_at": row[4],
                "created_at": row[5],
                "description": row[6],
                "done_at": row[7],
                "repeat": row[8],
                "repeated": row[9],
            }
            for row in rows
        ]

    def set_status(
        self, identity: Identity | Iterable[Identity], task_id: int, status: str
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = %s,
                        done_at = CASE WHEN %s = 'done'
                                       THEN CURRENT_TIMESTAMP END
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (status, status, task_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def set_priority(
        self,
        identity: Identity | Iterable[Identity],
        task_id: int,
        priority: int | None,
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET priority = %s
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (priority, task_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def set_due(
        self,
        identity: Identity | Iterable[Identity],
        task_id: int,
        due_at: datetime | None,
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET due_at = %s,
                        notified_at = CASE WHEN due_at IS DISTINCT FROM %s
                                           THEN NULL ELSE notified_at END
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (due_at, due_at, task_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def set_text(
        self,
        identity: Identity | Iterable[Identity],
        task_id: int,
        text: str,
        description: str | None,
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET text = %s, description = %s
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (text, description, task_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def set_repeat(
        self, identity: Identity | Iterable[Identity], task_id: int, repeat: str | None
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET repeat = %s
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (repeat, task_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def mark_repeated(self, task_id: int) -> None:
        """Record that a done task has made its next one, so it makes no more."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET repeated = TRUE WHERE id = %s", (task_id,)
                )
            conn.commit()

    def delete(self, identity: Identity | Iterable[Identity], task_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tasks WHERE id = %s AND user_id = ANY(%s)",
                    (task_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def due_soon(self, now: datetime, horizon: datetime) -> list[dict]:
        """Tasks nearing their deadline that have not been reminded yet.

        Across every account, because the worker has no single person's view:
        it wakes once an hour and asks what is due for anybody. A task is
        returned when its deadline falls inside ``(now, horizon]`` and
        ``notified_at`` is NULL.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, text, due_at
                    FROM tasks
                    WHERE due_at IS NOT NULL
                      AND due_at > %s
                      AND due_at <= %s
                      AND notified_at IS NULL
                      AND status <> 'done'
                    ORDER BY due_at
                    """,
                    (now, horizon),
                )
                rows = cur.fetchall()

        return [
            {"id": row[0], "user_id": row[1], "text": row[2], "due_at": row[3]}
            for row in rows
        ]

    def mark_notified(self, task_id: int) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET notified_at = %s WHERE id = %s",
                    (datetime.now(), task_id),
                )
            conn.commit()
