from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from app.core.identity import Identity

from .database import Database


def _as_tuple(identity: Identity | Iterable[Identity]) -> tuple[Identity, ...]:
    """Accept one identity or several, so callers can widen a read."""
    if isinstance(identity, Identity):
        return (identity,)

    return tuple(identity)


class ExpensesRepository:
    """Spending, its categories, and the aggregation both are read through.

    Money crosses this boundary as Decimal on the way in and on the way out.
    Converting at the edge keeps the binary-floating-point error that NUMERIC
    was chosen to avoid from reappearing in Python.
    """

    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    # -- categories ---------------------------------------------------------

    def add_category(
        self,
        identity: Identity | Iterable[Identity],
        name: str,
    ) -> int:
        """Create a category at the end of the list, returning its id."""
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                # A widened read takes part in writes too, so a linked pair
                # adding a category does not split the name across two
                # namespaces and collide with itself.
                cur.execute(
                    """
                    INSERT INTO expense_categories (user_id, name, position)
                    VALUES (%s, %s, COALESCE((
                        SELECT MAX(position) + 1
                        FROM expense_categories
                        WHERE user_id = ANY(%s)
                    ), 1))
                    RETURNING id
                    """,
                    (str(identities[0]), name, [str(one) for one in identities]),
                )
                category_id = cur.fetchone()[0]
            conn.commit()

        return category_id

    def categories(
        self,
        identity: Identity | Iterable[Identity],
        include_archived: bool = False,
    ) -> list[dict]:
        """Every category, in the order the account arranged them.

        A category that is still referenced by expenses is reported with the
        count, so the interface can say whether retiring it would leave rows
        behind rather than letting the user discover it afterwards.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.name, c.position, c.is_archived,
                           count(e.id) AS expenses_count,
                           COALESCE(SUM(e.amount), 0) AS total
                    FROM expense_categories c
                    LEFT JOIN expenses e
                        ON e.category_id = c.id AND e.user_id = c.user_id
                    WHERE c.user_id = ANY(%s)
                        AND (%s OR NOT c.is_archived)
                    GROUP BY c.id
                    ORDER BY c.position, c.id
                    """,
                    ([str(one) for one in identities], include_archived),
                )

                return [
                    {
                        "id": row[0],
                        "name": row[1],
                        "position": row[2],
                        "is_archived": row[3],
                        "expenses_count": row[4],
                        "total": Decimal(row[5]),
                    }
                    for row in cur.fetchall()
                ]

    def archive_category(
        self,
        identity: Identity | Iterable[Identity],
        category_id: int,
    ) -> bool:
        """Retire a category, keeping whatever it already holds.

        A delete would cascade into the expenses and silently rewrite a month's
        total. Returns whether the category was there and still active.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE expense_categories
                    SET is_archived = TRUE
                    WHERE id = %s
                        AND user_id = ANY(%s)
                        AND NOT is_archived
                    """,
                    (category_id, [str(one) for one in identities]),
                )
                archived = cur.rowcount
            conn.commit()

        return archived > 0

    # -- expenses -----------------------------------------------------------

    def add(
        self,
        identity: Identity,
        category_id: int,
        amount: Decimal,
        spent_at: date,
        note: str = "",
    ) -> None:
        """Record one expense against a category of this account's own.

        Scoped on the category as well as on the account: the id alone is a
        global sequence, and accepting someone else's would file this expense
        in another account's totals.
        """
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO expenses
                        (user_id, category_id, amount, spent_at, note)
                    SELECT %s, id, %s, %s, %s
                    FROM expense_categories
                    WHERE id = %s AND user_id = ANY(%s)
                    """,
                    (
                        str(identity),
                        amount,
                        spent_at,
                        note,
                        category_id,
                        [str(identity)],
                    ),
                )
                inserted = cur.rowcount
            conn.commit()

        if not inserted:
            raise ValueError("No such category for this account")

    def recent(
        self,
        identity: Identity | Iterable[Identity],
        limit: int = 50,
    ) -> list[dict]:
        """The most recently spent entries, newest first.

        Bounded because this feeds a list on a page: the analytics read is the
        aggregation below, and a month of expenses should not be a query whose
        result grows without limit.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.id, e.amount, e.spent_at, e.note, c.name
                    FROM expenses e
                    JOIN expense_categories c ON c.id = e.category_id
                    WHERE e.user_id = ANY(%s)
                    ORDER BY e.spent_at DESC, e.id DESC
                    LIMIT %s
                    """,
                    ([str(one) for one in identities], limit),
                )

                return [
                    {
                        "id": row[0],
                        "amount": Decimal(row[1]),
                        "spent_at": row[2],
                        "note": row[3],
                        "category": row[4],
                    }
                    for row in cur.fetchall()
                ]

    def delete(self, identity: Identity, expense_id: int) -> bool:
        """Remove one entry, reporting whether it was there."""
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM expenses WHERE id = %s AND user_id = %s",
                    (expense_id, str(identity)),
                )
                deleted = cur.rowcount
            conn.commit()

        return deleted > 0

    def by_category(
        self,
        identity: Identity | Iterable[Identity],
        month: str,
    ) -> list[dict]:
        """One row per category for a month, largest first.

        ``month`` is a ``YYYY-MM`` string. The range is built from
        ``date_trunc`` and closed with an interval, so the last day is
        whatever the month actually has rather than the 30th. Both bounds are
        computed in UTC: the database runs in UTC and the person reading the
        report is not, and a month boundary shifted by an offset puts the
        first expense of the month into the previous one.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.name, SUM(e.amount) AS total, count(e.id) AS count
                    FROM expenses e
                    JOIN expense_categories c ON c.id = e.category_id
                    WHERE e.user_id = ANY(%s)
                        AND date_trunc('month', e.spent_at)
                            = to_date(%s || '-01', 'YYYY-MM-DD')
                    GROUP BY c.id, c.name
                    ORDER BY total DESC
                    """,
                    ([str(one) for one in identities], month),
                )

                return [
                    {"category": row[0], "total": Decimal(row[1]), "count": row[2]}
                    for row in cur.fetchall()
                ]

    def monthly_total(
        self,
        identity: Identity | Iterable[Identity],
        month: str,
    ) -> Decimal:
        """Everything spent in a month, across all categories."""
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM expenses
                    WHERE user_id = ANY(%s)
                        AND date_trunc('month', spent_at)
                            = to_date(%s || '-01', 'YYYY-MM-DD')
                    """,
                    ([str(one) for one in identities], month),
                )

                return Decimal(cur.fetchone()[0])

    def available_months(
        self,
        identity: Identity | Iterable[Identity],
    ) -> list[str]:
        """Months that hold at least one expense, newest first.

        The dropdown is built from what is actually there, so it cannot offer a
        month with nothing in it and cannot omit one that has data.
        """
        identities = _as_tuple(identity)

        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT to_char(spent_at, 'YYYY-MM')
                    FROM expenses
                    WHERE user_id = ANY(%s)
                    ORDER BY 1 DESC
                    """,
                    ([str(one) for one in identities],),
                )

                return [row[0] for row in cur.fetchall()]
