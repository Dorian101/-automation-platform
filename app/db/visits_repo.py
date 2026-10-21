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
#: the first few dozen rows or it is not here at all. The rest is reached by
#: turning the page, not by scrolling.
DEFAULT_LIMIT = 50

#: How long anything is kept. A deployment that wants a different number
#: changes this one and runs `prune-visits`.
RETENTION_DAYS = 90


@dataclass(frozen=True)
class Visit:
    at: datetime
    kind: str
    path: str
    identity: str | None
    referrer: str | None = None


def _where(path: str, exclude: str | None) -> tuple[str, list]:
    """The conditions a query of the log is cut by, and what to fill them with.

    One place, so that counting and reading cut the same rows. The exclusion
    is written as `IS DISTINCT FROM` rather than `!=` because a visit by a
    reader with no session has no identity at all, and `NULL != 'web:somebody'`
    is not true in SQL — which would have left every anonymous row out of the
    count the moment the owner asked to be hidden.
    """
    conditions: list[str] = []
    arguments: list = []

    if path:
        conditions.append("path = %s")
        arguments.append(path)

    if exclude is not None:
        conditions.append("identity IS DISTINCT FROM %s")
        arguments.append(exclude)

    if not conditions:
        return "", arguments

    return "WHERE " + " AND ".join(conditions), arguments


class VisitsRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def record(
        self,
        kind: str,
        path: str = "",
        identity: str | None = None,
        referrer: str | None = None,
    ) -> None:
        """Write one event.

        Called on the way out of a public request, which is the only place that
        knows an event happened without every handler having to say so. A
        failure here is swallowed: a log that refuses to write must not take
        the page it is describing down with it.

        `referrer` is the origin that led here, already shortened from the
        header by the caller; None means there was no referrer.
        """
        try:
            with self.database.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO visits (kind, path, identity, referrer) "
                        "VALUES (%s, %s, %s, %s)",
                        (kind, path, identity, referrer),
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
        offset: int = 0,
        exclude: str | None = None,
    ) -> list[Visit]:
        """The newest events, optionally only those of one page.

        `offset` walks the log a page at a time, and `exclude` drops one
        identity from the rows. The exclusion belongs here rather than in the
        reader, because a filter applied after the page was chosen leaves two
        different numbers in the room: the rows on the page and the rows the
        page was cut from. `count` takes the same exclusion for the same
        reason.
        """
        where, arguments = _where(path, exclude)
        arguments = arguments + [limit, offset]

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT at, kind, path, identity, referrer FROM visits "
                    f"{where} "
                    "ORDER BY at DESC, id DESC "
                    "LIMIT %s OFFSET %s",
                    arguments,
                )
                rows = cur.fetchall()

        return [Visit(at=row[0], kind=row[1], path=row[2], identity=row[3],
                      referrer=row[4])
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

    def count(self, path: str = "", exclude: str | None = None) -> int:
        """How many events the log holds, on the same terms as `recent`.

        The exclusion is the same one, so the number and the rows it counts
        are the same rows. A total that included what the table hides is a
        number nobody can match against the table under it.
        """
        where, arguments = _where(path, exclude)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM visits {where}", arguments)
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
