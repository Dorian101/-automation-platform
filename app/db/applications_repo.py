from collections.abc import Iterable
from datetime import date

from app.core.identity import Identity

from .database import Database


def _as_tuple(identity: Identity | Iterable[Identity]) -> tuple[Identity, ...]:
    """Accept one identity or several, so callers can widen a read."""
    if isinstance(identity, Identity):
        return (identity,)

    return tuple(identity)


class ApplicationsRepository:
    """Job applications and the counts the funnel is drawn from.

    Stores and reads only. Which stage may follow which is the plugin's
    business — a repository that enforced transitions would be deciding
    something about the feature rather than about storage — so
    :meth:`set_status` checks nothing and the CHECK in the schema is what
    keeps a stored stage real.
    """

    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(
        self,
        identity: Identity,
        company: str,
        role: str,
        applied_at: date,
        url: str = "",
        note: str = "",
    ) -> int:
        """Record one application in its opening stage, returning its id."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_applications
                        (user_id, company, role, url, applied_at, note)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        str(identity),
                        company,
                        role,
                        url or None,
                        applied_at,
                        note,
                    ),
                )
                application_id = cur.fetchone()[0]
            conn.commit()

        return application_id

    def entries(
        self,
        identity: Identity | Iterable[Identity],
    ) -> list[dict]:
        """Every application, newest first.

        Widened across a linked pair the same way notes and expenses are, so
        an application added in the bot is listed in the console.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, company, role, url, status, applied_at, note
                    FROM job_applications
                    WHERE user_id = ANY(%s)
                    ORDER BY applied_at DESC, id DESC
                    """,
                    ([str(one) for one in identities],),
                )

                return [
                    {
                        "id": row[0],
                        "company": row[1],
                        "role": row[2],
                        "url": row[3],
                        "status": row[4],
                        "applied_at": row[5],
                        "note": row[6],
                    }
                    for row in cur.fetchall()
                ]

    def set_status(
        self,
        identity: Identity,
        application_id: int,
        status: str,
    ) -> bool:
        """Move one application to a stage, reporting whether it was there.

        Scoped to the account as well as to the id. The id is a global
        sequence, so accepting one that is not this account's would let a
        guess rewrite somebody else's stage.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_applications
                    SET status = %s
                    WHERE id = %s AND user_id = %s
                    """,
                    (status, application_id, str(identity)),
                )
                updated = cur.rowcount
            conn.commit()

        return updated > 0

    def delete(self, identity: Identity, application_id: int) -> bool:
        """Remove one application, reporting whether it was there."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM job_applications WHERE id = %s AND user_id = %s",
                    (application_id, str(identity)),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0

    def counts_by_status(
        self,
        identity: Identity | Iterable[Identity],
    ) -> dict[str, int]:
        """How many applications sit in each stage.

        A single group-by, which is the whole of the funnel's arithmetic:
        the page turns these numbers into shares rather than counting rows
        itself, so the chart and the table below it cannot disagree.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, count(*)
                    FROM job_applications
                    WHERE user_id = ANY(%s)
                    GROUP BY status
                    """,
                    ([str(one) for one in identities],),
                )

                return {row[0]: row[1] for row in cur.fetchall()}
