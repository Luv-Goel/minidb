"""Demo: feature tour printing real query output.

Run:  python examples/demo.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minidb.engine import Database
from minidb.repl import format_table


def show(db: Database, sql: str, note: str = "") -> None:
    print(f"minidb> {sql}")
    r = db.execute(sql)
    if r.kinds == "select":
        print(format_table(r.columns, r.rows))
        print(f"{len(r.rows)} row(s)\n")
    else:
        print(f"ok, {r.rowcount} row(s)\n")


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(prefix="minidb-demo-"), "demo.db")
    db = Database(db_path)
    print("=== DDL ===")
    show(db, "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
    show(db, "CREATE TABLE orders (oid INTEGER, user_id INTEGER, amount INTEGER)")
    show(db, "INSERT INTO users VALUES (1, 'ada', 36), (2, 'grace', 85), "
             "(3, 'linus', 55), (4, 'guido', 68)")
    show(db, "INSERT INTO orders VALUES (100, 1, 50), (101, 1, 70), "
             "(102, 3, 10), (103, 4, 200)")

    print("=== queries ===")
    show(db, "SELECT name, age FROM users WHERE age >= 50 ORDER BY age DESC")
    show(db, "SELECT users.name, SUM(orders.amount) AS total "
             "FROM users JOIN orders ON orders.user_id = users.id "
             "GROUP BY users.name ORDER BY total DESC LIMIT 3")
    show(db, "SELECT name FROM users WHERE name LIKE 'g%' AND age IN (68, 85)")

    print("=== transactions ===")
    show(db, "BEGIN")
    show(db, "UPDATE users SET age = 0 WHERE id = 3")
    show(db, "SELECT age FROM users WHERE id = 3", "inside the tx: age is 0")
    show(db, "ROLLBACK")
    show(db, "SELECT age FROM users WHERE id = 3", "after rollback: restored")

    print("=== durability: data survives process restarts ===")
    db.close()
    db = Database(db_path)
    show(db, "SELECT COUNT(*) AS total_users FROM users")
    print("data file:", db_path)
    print("WAL file: ", db_path + ".wal")
    db.close()


if __name__ == "__main__":
    main()
