"""Storage for tasks, the web-only to-do pile.

Follows the notes repository's shape: identity-scoped writes, reads widened
across a linked pair, no shared namespace. A task belongs to the identity that
wrote it and a read may span the pair, the same rule every personal plugin
already uses.
"""

from collections.abc import Iterable
from datetime import datetime

from app.core.identity import Identity

from .database import Database

# The fixed statuses. `todo` is the default and the pile; `doing` and `done` are
# the two ways a task stops being a bare reminder of itself.
STATUSES = ("todo", "doing", "done")

# `priority` is 1, 2 or 3, one being the highest. Any other integer is noise
# that would sort unpredictably, so the repository is not asked to store it.
VALID_PRIORITIES = {1, 2, 3}


def _as_tuple(identity: Identity | Iterable[Identity]) -> tuple[Identity, ...]:
    if isinstance(identity, Identity):
        return (identity,)

    return tuple(identity)


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
    ) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tasks (user_id, text, priority, due_at, status)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (str(identity), text, priority, due_at, status),
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
        identities = _as_tuple(identity)

        where = ""
        arguments: list = [[str(one) for one in identities]]

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
                    SELECT id, text, status, priority, due_at, created_at
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
            }
            for row in rows
        ]

    def set_status(self, identity: Identity, task_id: int, status: str) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = %s
                    WHERE id = %s AND user_id = %s
                    """,
                    (status, task_id, str(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def set_priority(
        self, identity: Identity, task_id: int, priority: int | None
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET priority = %s
                    WHERE id = %s AND user_id = %s
                    """,
                    (priority, task_id, str(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def set_due(
        self, identity: Identity, task_id: int, due_at: datetime | None
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET due_at = %s
                    WHERE id = %s AND user_id = %s
                    """,
                    (due_at, task_id, str(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def delete(self, identity: Identity, task_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tasks WHERE id = %s AND user_id = %s",
                    (task_id, str(identity)),
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
