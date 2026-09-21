"""Lexer and parser unit tests."""

import unittest

from minidb import ast
from minidb.errors import ParseError
from minidb.lexer import tokenize
from minidb.parser import parse


class LexerTest(unittest.TestCase):
    def test_keywords_case_insensitive(self):
        toks = tokenize("SELECT x FROM T WHERE y = 5")
        self.assertEqual(toks[0].kind, "keyword")
        self.assertEqual(toks[1].text, "x")
        self.assertEqual(toks[-1].kind, "eof")

    def test_string_escapes(self):
        (tok,) = [t for t in tokenize("'it''s'") if t.kind == "string"]
        self.assertEqual(tok.text, "it's")

    def test_operators(self):
        texts = [t.text for t in tokenize("a <= b <> c || d") if t.kind == "op"]
        self.assertEqual(texts, ["<=", "<>", "||"])

    def test_line_comment_skipped(self):
        toks = [t for t in tokenize("SELECT 1 -- note\nFROM t") if t.kind != "eof"]
        self.assertEqual([t.text for t in toks], ["SELECT", "1", "FROM", "t"])

    def test_unterminated_string(self):
        with self.assertRaises(ParseError):
            tokenize("'oops")


class ParserTest(unittest.TestCase):
    def test_select_star(self):
        stmt = parse("SELECT * FROM users;")
        self.assertIsInstance(stmt, ast.Select)
        self.assertEqual(stmt.from_table.name, "users")
        self.assertIsInstance(stmt.columns[0].expr, ast.Column)

    def test_select_full(self):
        stmt = parse(
            "SELECT u.name, COUNT(*) AS n FROM users u "
            "JOIN orders o ON o.user_id = u.id "
            "WHERE u.age >= 18 AND u.name LIKE 'a%' "
            "GROUP BY u.name HAVING COUNT(*) > 2 "
            "ORDER BY n DESC LIMIT 10 OFFSET 5")
        self.assertEqual(len(stmt.joins), 1)
        self.assertEqual(stmt.limit, 10)
        self.assertEqual(stmt.offset, 5)
        self.assertIsInstance(stmt.having, ast.BinOp)
        self.assertEqual(stmt.order_by[0][1], "desc")

    def test_insert_multirow(self):
        stmt = parse("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')")
        self.assertEqual(len(stmt.rows), 2)
        self.assertEqual(stmt.columns, ["a", "b"])

    def test_predicate_stack(self):
        stmt = parse("SELECT * FROM t WHERE a BETWEEN 1 AND 10 AND b IN (1, 2) "
                     "AND c IS NOT NULL")
        self.assertIsInstance(stmt.where, ast.And)

    def test_update_and_delete(self):
        u = parse("UPDATE t SET a = 1, b = b + 2 WHERE c <> 'x'")
        self.assertEqual(len(u.assignments), 2)
        d = parse("DELETE FROM t WHERE a < 0")
        self.assertIsInstance(d.where, ast.BinOp)

    def test_create_drop(self):
        c = parse("CREATE TABLE IF NOT EXISTS t (id INTEGER, name TEXT)")
        self.assertTrue(c.if_not_exists)
        d = parse("DROP TABLE IF EXISTS t")
        self.assertTrue(d.if_exists)

    def test_transactions(self):
        for kw, cls in (("BEGIN", ast.Begin), ("COMMIT", ast.Commit),
                        ("ROLLBACK", ast.Rollback)):
            self.assertIsInstance(parse(kw), cls)

    def test_trailing_garbage_rejected(self):
        with self.assertRaises(ParseError):
            parse("SELECT * FROM t WHERE")

    def test_join_alias(self):
        stmt = parse("SELECT * FROM users AS u JOIN orders o ON o.u = u.id")
        self.assertEqual(stmt.from_table.alias, "u")
        self.assertEqual(stmt.joins[0].table.alias, "o")


if __name__ == "__main__":
    unittest.main()
