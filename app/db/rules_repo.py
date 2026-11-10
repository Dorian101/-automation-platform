from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from psycopg.types.json import Jsonb

from app.core.identity import Identity, ids_of

from .database import Database

KEEP_RUNS = 200

COLUMNS = """
    id, user_id, name, trigger, schedule, condition_text, condition_amount,
    action, params, enabled, last_fired_on
"""


def _rule(row) -> dict:
    return {
        "id": row[0],
        "user_id": row[1],
        "name": row[2],
        "trigger": row[3],
        "schedule": row[4] or {},
        "condition_text": row[5],
        "condition_amount": Decimal(row[6]) if row[6] is not None else None,
        "action": row[7],
        "params": row[8] or {},
        "enabled": row[9],
        "last_fired_on": row[10],
    }


class RulesRepository:
    def __init__(self, database: Database | None = None):
        self.database = database or Database()

    def add(
        self,
        identity: Identity,
        name: str,
        trigger: str,
        action: str,
        params: dict,
        schedule: dict | None = None,
        condition_text: str | None = None,
        condition_amount: Decimal | None = None,
    ) -> int:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rules (user_id, name, trigger, schedule,
                                       condition_text, condition_amount,
                                       action, params)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        str(identity),
                        name,
                        trigger,
                        Jsonb(schedule) if schedule else None,
                        condition_text,
                        condition_amount,
                        action,
                        Jsonb(params),
                    ),
                )
                rule_id = cur.fetchone()[0]
            conn.commit()

        return rule_id

    def entries(self, identity: Identity | Iterable[Identity]) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM rules WHERE user_id = ANY(%s) ORDER BY id",
                    (ids_of(identity),),
                )
                return [_rule(row) for row in cur.fetchall()]

    def listening(
        self, identity: Identity | Iterable[Identity], trigger: str
    ) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM rules "
                    "WHERE user_id = ANY(%s) AND trigger = %s AND enabled "
                    "ORDER BY id",
                    (ids_of(identity), trigger),
                )
                return [_rule(row) for row in cur.fetchall()]

    def scheduled(self) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {COLUMNS} FROM rules "
                    "WHERE trigger = 'schedule' AND enabled ORDER BY id"
                )
                return [_rule(row) for row in cur.fetchall()]

    def set_enabled(
        self, identity: Identity | Iterable[Identity], rule_id: int, enabled: bool
    ) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE rules SET enabled = %s WHERE id = %s AND user_id = ANY(%s)",
                    (enabled, rule_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def delete(self, identity: Identity | Iterable[Identity], rule_id: int) -> bool:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM rules WHERE id = %s AND user_id = ANY(%s)",
                    (rule_id, ids_of(identity)),
                )
                changed = cur.rowcount
            conn.commit()

        return changed > 0

    def mark_fired(self, rule_id: int, day: date) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE rules SET last_fired_on = %s WHERE id = %s",
                    (day, rule_id),
                )
            conn.commit()

    def log(self, rule: dict, ok: bool, message: str) -> None:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO rule_runs (rule_id, user_id, ok, message) "
                    "VALUES (%s, %s, %s, %s)",
                    (rule["id"], rule["user_id"], ok, message),
                )
                cur.execute(
                    """
                    DELETE FROM rule_runs
                    WHERE user_id = %s AND id <= (
                        SELECT id FROM rule_runs WHERE user_id = %s
                        ORDER BY id DESC OFFSET %s LIMIT 1
                    )
                    """,
                    (rule["user_id"], rule["user_id"], KEEP_RUNS),
                )
            conn.commit()

    def runs(
        self, identity: Identity | Iterable[Identity], limit: int = 100
    ) -> list[dict]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.at, r.ok, r.message, s.name
                    FROM rule_runs r JOIN rules s ON s.id = r.rule_id
                    WHERE r.user_id = ANY(%s)
                    ORDER BY r.id DESC
                    LIMIT %s
                    """,
                    (ids_of(identity), limit),
                )
                return [
                    {"at": row[0], "ok": row[1], "message": row[2], "rule": row[3]}
                    for row in cur.fetchall()
                ]

    def counts(self, identity: Identity | Iterable[Identity]) -> dict[int, tuple]:
        with self.database.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rule_id, count(*), max(at)
                    FROM rule_runs WHERE user_id = ANY(%s)
                    GROUP BY rule_id
                    """,
                    (ids_of(identity),),
                )
                return {row[0]: (row[1], row[2]) for row in cur.fetchall()}
