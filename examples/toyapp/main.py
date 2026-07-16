"""Entry point: register then look up a user."""
from .db import DB
from .service import register, lookup

def main():
    db = DB()
    register(db, "ada")
    return lookup(db, 1)

if __name__ == "__main__":
    print(main())
