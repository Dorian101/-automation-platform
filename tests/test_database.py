from app.db.database import Database


def test_connect_after_close_reopens_the_pool(database_with_schema):
    """Closing the pool must not leave the instance unusable.

    `main.py` closes the pool on shutdown; a caller that then asks for a
    connection again (a process reused across a restart, or a test) has to get
    a working one rather than one pointing at a closed pool.
    """
    db = Database()

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1

    db.close()

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 2")
            assert cur.fetchone()[0] == 2

    db.close()


def test_pool_is_shared_by_borrowers(database_with_schema):
    """One ``Database`` is one pool, so borrowing twice is not two servers."""
    db = Database()

    with db.connect() as conn:
        pid = None
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            # The first connection was returned to the pool and is lent out
            # again rather than a fresh one being opened.
            assert cur.fetchone()[0] == pid

    db.close()
