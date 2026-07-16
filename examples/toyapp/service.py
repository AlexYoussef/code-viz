"""Service layer — calls into the db layer."""
from .db import DB, save_user, get_user

def register(db: DB, name: str) -> None:
    save_user(db, name)

def lookup(db: DB, uid: int):
    return get_user(db, uid)
