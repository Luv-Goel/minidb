"""End-to-end SQL tests: DDL, CRUD, queries, transactions, crash recovery."""

import os
import tempfile
import unittest
from pathlib import Path

from minidb.engine import Database
from minidb.errors import DatabaseError, ExecutionError, SchemaError


class DatabaseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def make_users(self):
        self.db.execute(
            "CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
        self.db.execute(
            "INSERT INTO users VALUES (1, 'ada', 36), (2, 'grace', 85), "
            "(3, 'linus', 55), (4, 'guido', 68)")


class DDLTest(DatabaseTestBase):
    def test_create_and_insert_and_select(self):
        self.make_users()
        r = self.db.execute("SELECT * FROM users ORDER BY id")
        self.assertEqual(r.columns, ["id", "name", "age"])
        self.assertEqual(r.rows[0], (1, "ada", 36))
        self.assertEqual(len(r.rows), 4)

    def test_duplicate_table_rejected(self):
        self.db.execute("CREATE TABLE t (a INTEGER)")
        with self.assertRaises(SchemaError):
            self.db.execute("CREATE TABLE t (a INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")  # no-op

    def test_drop_table(self):
        self.db.execute("CREATE TABLE t (a INTEGER)")
        self.db.execute("DROP TABLE t")
        with self.assertRaises(SchemaError):
            self.db.execute("SELECT * FROM t")
        self.db.execute("DROP TABLE IF EXISTS t")  # no error

    def test_type_enforcement(self):
        self.db.execute("CREATE TABLE t (a INTEGER)")
        with self.assertRaises(ExecutionError):
            self.db.execute("INSERT INTO t VALUES ('not an int')")

    def test_column_count_check(self):
        self.make_users()
        with self.assertRaises(ExecutionError):
            self.db.execute("INSERT INTO users VALUES (9, 'x')")


class QueryTest(DatabaseTestBase):
    def setUp(self):
        super().setUp()
        self.make_users()

    def test_where_filters(self):
        r = self.db.execute("SELECT name FROM users WHERE age < 70 "
                            "ORDER BY name")
        self.assertEqual(r.rows, [("ada",), ("guido",), ("linus",)])

    def test_like_and_in(self):
        r = self.db.execute(
            "SELECT name FROM users WHERE name LIKE 'g%' AND age IN (85, 68)")
        self.assertEqual(sorted(r[0] for r in r.rows), ["grace", "guido"])

    def test_between_and_null(self):
        r = self.db.execute("SELECT id FROM users WHERE age BETWEEN 30 AND 60")
        self.assertEqual([r[0] for r in r.rows], [1, 3])
        self.db.execute("INSERT INTO users VALUES (5, NULL, 42)")
        r = self.db.execute("SELECT id FROM users WHERE name IS NULL")
        self.assertEqual(r.rows, [(5,)])

    def test_arithmetic_and_aliasing(self):
        r = self.db.execute(
            "SELECT name, age + 1 AS next_year FROM users WHERE id = 1")
        self.assertEqual(r.columns, ["name", "next_year"])
        self.assertEqual(r.rows, [("ada", 37)])

    def test_aggregates(self):
        r = self.db.execute("SELECT COUNT(*), MIN(age), MAX(age), SUM(age) "
                            "FROM users")
        self.assertEqual(r.rows, [(4, 36, 85, 244)])

    def test_group_by_having(self):
        self.db.execute(
            "INSERT INTO users VALUES (5, 'ada-jr', 10), (6, 'ada-sr', 70)")
        r = self.db.execute(
            "SELECT name LIKE 'ada%', COUNT(*) AS n FROM users "
            "GROUP BY name LIKE 'ada%' HAVING COUNT(*) > 1 ORDER BY n DESC")
        self.assertEqual(sorted(row[1] for row in r.rows), [3, 3])

    def test_join(self):
        self.db.execute("CREATE TABLE orders (oid INTEGER, user_id INTEGER, "
                        "amount INTEGER)")
        self.db.execute("INSERT INTO orders VALUES (100, 1, 50), "
                        "(101, 1, 70), (102, 3, 10)")
        r = self.db.execute(
            "SELECT users.name, SUM(orders.amount) AS total "
            "FROM users JOIN orders ON orders.user_id = users.id "
            "GROUP BY users.name ORDER BY total DESC")
        self.assertEqual(r.rows[0], ("ada", 120))
        self.assertEqual(r.rows[-1], ("linus", 10))

    def test_update(self):
        n = self.db.execute(
            "UPDATE users SET age = age + 1 WHERE name = 'ada'")
        self.assertEqual(n.rowcount, 1)
        r = self.db.execute("SELECT age FROM users WHERE id = 1")
        self.assertEqual(r.rows, [(37,)])

    def test_delete(self):
        n = self.db.execute("DELETE FROM users WHERE age > 60")
        self.assertEqual(n.rowcount, 2)
        r = self.db.execute("SELECT COUNT(*) FROM users")
        self.assertEqual(r.rows, [(2,)])

    def test_distinct_limit_offset(self):
        r = self.db.execute(
            "SELECT DISTINCT age FROM users ORDER BY age DESC LIMIT 2")
        self.assertEqual(r.rows, [(85,), (68,)])
        r = self.db.execute(
            "SELECT age FROM users ORDER BY age LIMIT 2 OFFSET 1")
        self.assertEqual(r.rows, [(55,), (68,)])

    def test_scalar_functions(self):
        r = self.db.execute(
            "SELECT UPPER(name), LENGTH(name) FROM users WHERE id = 1")
        self.assertEqual(r.rows, [("ADA", 3)])

    def test_many_rows_stress_btree(self):
        values = ", ".join(f"({i}, 'user{i}', {i % 100})"
                           for i in range(1, 1501))
        self.db.execute(f"INSERT INTO users VALUES {values}")
        r = self.db.execute("SELECT COUNT(*) FROM users")
        self.assertEqual(r.rows, [(1504,)])
        r = self.db.execute(
            "SELECT name FROM users WHERE id = 1499")
        self.assertEqual(r.rows, [("user1499",)])


class TransactionTest(DatabaseTestBase):
    def test_rollback_restores_state(self):
        self.make_users()
        self.db.execute("BEGIN")
        self.db.execute("UPDATE users SET age = 0")
        self.db.execute("DELETE FROM users WHERE id = 1")
        self.db.execute("ROLLBACK")
        r = self.db.execute("SELECT COUNT(*), SUM(age) FROM users")
        self.assertEqual(r.rows, [(4, 244)])

    def test_commit_persists(self):
        self.make_users()
        self.db.execute("BEGIN")
        self.db.execute("DELETE FROM users WHERE id = 4")
        self.db.execute("INSERT INTO users VALUES (7, 'margaret', 87)")
        self.db.execute("COMMIT")
        r = self.db.execute("SELECT COUNT(*) FROM users")
        self.assertEqual(r.rows, [(4,)])
        r = self.db.execute("SELECT name FROM users WHERE id = 7")
        self.assertEqual(r.rows, [("margaret",)])

    def test_nested_begin_rejected(self):
        self.db.execute("BEGIN")
        with self.assertRaises(DatabaseError):
            self.db.execute("BEGIN")
        self.db.execute("ROLLBACK")

    def test_commit_without_begin_rejected(self):
        with self.assertRaises(DatabaseError):
            self.db.execute("COMMIT")

    def test_isolation_inside_tx(self):
        """Writes are visible inside the tx, invisible after rollback."""
        self.make_users()
        self.db.execute("BEGIN")
        self.db.execute("UPDATE users SET name = 'replaced' WHERE id = 2")
        r = self.db.execute("SELECT name FROM users WHERE id = 2")
        self.assertEqual(r.rows, [("replaced",)])
        self.db.execute("ROLLBACK")
        r = self.db.execute("SELECT name FROM users WHERE id = 2")
        self.assertEqual(r.rows, [("grace",)])


class DurabilityTest(DatabaseTestBase):
    def reopen(self):
        self.db.close()
        self.db = Database(self.path)

    def test_committed_data_survives_reopen(self):
        self.make_users()
        self.reopen()
        r = self.db.execute("SELECT COUNT(*) FROM users")
        self.assertEqual(r.rows, [(4,)])

    def test_crash_recovery_from_wal(self):
        """Crash after WAL-fsync but before checkpoint: WAL replay restores."""
        self.make_users()
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO users VALUES (50, 'crashy', 1)")
        # commit writes the WAL but we crash the pager before the checkpoint
        ops = [{"op": "put", "table": t, "rowid": rid, "row": row}
               for (t, rid), row in self.db.tx.redo.items()]
        self.db.wal.append(self.db.tx.txid, ops)     # durable WAL records
        self.db._simulate_crash()                    # dirty pages lost
        self.db = Database(self.path)                # recovery replays WAL
        r = self.db.execute("SELECT name FROM users WHERE id = 50")
        self.assertEqual(r.rows, [("crashy",)])

    def test_uncommitted_tx_lost_on_crash(self):
        self.make_users()
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO users VALUES (60, 'ghost', 99)")
        self.db._simulate_crash()                    # no COMMIT ever reached
        self.db = Database(self.path)
        r = self.db.execute("SELECT COUNT(*) FROM users")
        self.assertEqual(r.rows, [(4,)])


class JoinEdgeTest(DatabaseTestBase):
    def test_join_star_and_qualified_names(self):
        self.db.execute("CREATE TABLE a (id INTEGER, v TEXT)")
        self.db.execute("CREATE TABLE b (id INTEGER, a_id INTEGER, w TEXT)")
        self.db.execute("INSERT INTO a VALUES (1, 'x'), (2, 'y')")
        self.db.execute("INSERT INTO b VALUES (10, 1, 'p'), (11, 1, 'q')")
        r = self.db.execute(
            "SELECT a.v, b.w FROM a JOIN b ON b.a_id = a.id "
            "ORDER BY b.id DESC")
        self.assertEqual(r.rows, [("x", "q"), ("x", "p")])
        r = self.db.execute("SELECT * FROM a JOIN b ON b.a_id = a.id")
        self.assertEqual(r.columns, ["id", "v", "id", "a_id", "w"])
        self.assertEqual(len(r.rows), 2)


if __name__ == "__main__":
    unittest.main()
