"""What happened on the public side of the site, and when.

A log, not a table of people. Nothing here identifies a stranger: a reader
with no session is recorded as having arrived, and a demonstration guest is
recorded under a random token that names no account and stops existing when
the guest namespaces are pruned. There is no address, on the grounds that an
address would tell the owner nothing they did not already have — the useful
part of a visit is when it happened, and the way to answer "who was that" is
to line it up with a résumé site, not to have collected one.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .database import Database

#: The kinds of thing recorded. A view is a public page or a demonstration
#: page; the other two are what somebody did about an account.
VIEW = "view"
SIGNIN = "signin"
SIGNUP = "signup"

#: How many rows one page shows. A screen of events, not a report: the
#: question being answered is "was anybody here today", and the answer is in
#: the first few dozen rows or it is not here at all.
DEFAULT_LIMIT = 200

#: How long anything is kept. A deployment that wants a different number
#: changes this one and runs `prune-visits`.
RETENTION_DAYS = 90


@dataclass(frozen=True)
class Visit:
    at: datetime
    kind: str
    path: str
    identity: str | None


class VisitsRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def record(
        self,
        kind: str,
        path: str = "",
        identity: str | None = None,
    ) -> None:
        """Write one event.

        Called on the way out of a public request, which is the only place that
        knows an event happened without every handler having to say so. A
        failure here is swallowed: a log that refuses to write must not take
        the page it is describing down with it.
        """
        try:
            with self.database.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO visits (kind, path, identity) "
                        "VALUES (%s, %s, %s)",
                        (kind, path, identity),
                    )
                conn.commit()
        except Exception:
            # noqa: BLE001 — a visitor must not get an error page because
            # the thing that counted the visit is unwell.
            return

    def recent(
        self,
        path: str = "",
        limit: int = DEFAULT_LIMIT,
    ) -> list[Visit]:
        """The newest events, optionally only those of one page."""
        if path:
            query = """
                SELECT at, kind, path, identity FROM visits
                WHERE path = %s
                ORDER BY at DESC, id DESC
                LIMIT %s
            """
            arguments = (path, limit)
        else:
            query = """
                SELECT at, kind, path, identity FROM visits
                ORDER BY at DESC, id DESC
                LIMIT %s
            """
            arguments = (limit,)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, arguments)
                rows = cur.fetchall()

        return [Visit(at=row[0], kind=row[1], path=row[2], identity=row[3])
                for row in rows]

    def paths(self) -> list[str]:
        """Every page anyone has looked at, for the filter."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT path FROM visits "
                    "WHERE path <> '' ORDER BY path"
                )
                rows = cur.fetchall()

        return [row[0] for row in rows]

    def count(self, path: str = "") -> int:
        if path:
            with self.database.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM visits WHERE path = %s", (path,)
                    )
                    return cur.fetchone()[0]

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM visits")
                return cur.fetchone()[0]

    def prune(self, days: int = RETENTION_DAYS) -> int:
        """Delete anything older than the window; how many rows went."""
        cutoff = datetime.now() - timedelta(days=days)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM visits WHERE at < %s", (cutoff,))
                removed = cur.rowcount
            conn.commit()

        return removed
