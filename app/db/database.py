import psycopg
from psycopg_pool import ConnectionPool

from app.core.config import Config


class Database:
    """A shared pool of PostgreSQL connections.

    Every repository takes the one ``Database`` instance and asks it for a
    connection, so there is a single pool rather than one per caller. The pool
    is opened lazily — on the first ``connect()`` — and ``close()`` returns to
    it what it lent out, which is how the process shuts the pool down cleanly.

    ``connect()`` hands back a context manager: the same ``with
    database.connect() as conn`` every repository already wrote. Psycopg's
    pool commits on a clean exit and rolls back on an exception, so a query
    that raises leaves no half-open transaction for the next borrower.
    """

    def __init__(self) -> None:
        self._min_size = Config.DB_POOL_MIN_SIZE
        self._max_size = Config.DB_POOL_MAX_SIZE
        self._pool: ConnectionPool | None = None

    def connect(self):
        """Return a connection borrowed from the pool, opened on first use.

        Raises ``PoolTimeout`` when no connection is free within
        ``DB_POOL_TIMEOUT_SECONDS``, so a request that cannot be served fails
        fast rather than hanging the event loop on a full pool.
        """
        return self._get_pool().connection(timeout=Config.DB_POOL_TIMEOUT_SECONDS)

    def close(self) -> None:
        """Close the pool, returning every connection to the server."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(
                conninfo=_conninfo(),
                min_size=self._min_size,
                max_size=self._max_size,
                open=True,
            )

        return self._pool


def _conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        dbname=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
    )
