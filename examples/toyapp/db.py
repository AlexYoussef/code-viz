"""Tiny DB access layer (psycopg-style) for the code-viz demo."""
import psycopg

class DB:
    def __init__(self, dsn="postgresql://localhost/demo"):
        self._conn = psycopg.connect(dsn, autocommit=True)

    def query(self, sql, params=None):
        with self._conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

def save_user(db: DB, name: str) -> None:
    db.query("INSERT INTO app.users (name) VALUES (%s)", (name,))

def get_user(db: DB, uid: int):
    return db.query("SELECT id, name FROM app.users WHERE id = %s", (uid,))
