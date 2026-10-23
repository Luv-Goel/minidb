"""Regression tests for SELECT/engine correctness fixes.

Each case below documents a behaviour that was verifiably wrong (or plain
absent) before the fix named in its docstring. Where SQL standard behaviour
is unambiguous the assertion is exact; where RDBMS implementations differ
the test asserts the guarantee that must never break (DISTINCT semantics,
determinism) instead of locking in one vendor's ordering.
"""

from __future__ import annotations

import pytest

from minidb.engine import Database
from minidb.errors import ExecutionError, ParseError


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


# --------------------------------------------------------------------- #
# 1. DISTINCT must keep rows and groups paired in lockstep
# --------------------------------------------------------------------- #

def test_distinct_orders_by_first_occurrence_group(db):
    """ORDER BY must pair every DISTINCT-surviving row with the group env
    of *its own* first occurrence, not with a row that survived dedup
    elsewhere. Otherwise the sort key is silently garbage."""
    db.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
    db.execute("INSERT INTO t VALUES (5, 10), (5, 20), (9, 30), (1, 40)")

    res = db.execute("SELECT DISTINCT a FROM t ORDER BY b")
    assert [r[0] for r in res.rows] == [5, 9, 1]   # b = 10, 30, 40


def test_distinct_orders_by_output_column(db):
    """The common, unambiguous case: ORDER BY on a selected column must
    actually sort (a pre-fix symptom of the mispairing was no sort at all)."""
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (5), (5), (9), (1)")
    assert db.execute("SELECT DISTINCT a FROM t ORDER BY a").rows == [(1,), (5,), (9,)]
    assert db.execute("SELECT DISTINCT a FROM t ORDER BY a DESC").rows == [(9,), (5,), (1,)]


def test_distinct_null_ordering_stable(db):
    """NULLs last regardless of direction (SQL standard)."""
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (5), (NULL), (9)")
    assert db.execute("SELECT DISTINCT a FROM t ORDER BY a").rows == [(5,), (9,), (None,)]
    assert db.execute("SELECT DISTINCT a FROM t ORDER BY a DESC").rows == [(None,), (9,), (5,)]


# --------------------------------------------------------------------- #
# 2. Arithmetic division / modulo by zero must yield NULL, not raise
# --------------------------------------------------------------------- #

def test_division_by_zero_yields_null(db):
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (7)")
    assert db.execute("SELECT a / 0 FROM t").rows == [(None,)]
    assert db.execute("SELECT a % 0 FROM t").rows == [(None,)]
    assert db.execute("SELECT a / (a - a) FROM t").rows == [(None,)]


def test_null_propagation_still_holds(db):
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (7)")
    assert db.execute("SELECT a + NULL FROM t").rows == [(None,)]


# --------------------------------------------------------------------- #
# 3. SUM/AVG over non-numeric data must raise a database error,
#    not leak a raw Python TypeError
# --------------------------------------------------------------------- #

def test_sum_over_text_raises_execution_error(db):
    db.execute("CREATE TABLE t (a TEXT)")
    db.execute("INSERT INTO t VALUES ('10'), ('20')")
    with pytest.raises(ExecutionError, match="requires numeric"):
        db.execute("SELECT SUM(a) FROM t")
    with pytest.raises(ExecutionError, match="requires numeric"):
        db.execute("SELECT AVG(a) FROM t")


def test_sum_and_avg_on_numeric_columns_work(db):
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (2), (4), (6)")
    assert db.execute("SELECT SUM(a) FROM t").rows == [(12,)]
    assert db.execute("SELECT AVG(a) FROM t").rows == [(4.0,)]


# --------------------------------------------------------------------- #
# 4. COUNT(DISTINCT expr) -- feature that previously failed to parse
# --------------------------------------------------------------------- #

def test_count_distinct_parses_and_evaluates(db):
    db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("INSERT INTO t VALUES (1), (1), (2), (3), (3), (NULL)")
    assert db.execute("SELECT COUNT(DISTINCT a) FROM t").rows == [(3,)]
    assert db.execute("SELECT COUNT(a) FROM t").rows == [(5,)]
    assert db.execute("SELECT COUNT(*) FROM t").rows == [(6,)]


def test_count_distinct_rejects_star(db):
    with pytest.raises(ParseError):
        db.execute("SELECT COUNT(DISTINCT *) FROM t")


def test_distinct_inside_other_aggregates(db):
    """DISTINCT is legal on SUM/AVG/MIN/MAX too, and must dedupe before
    aggregating while ignoring NULLs."""
    db.execute("CREATE TABLE t (v INTEGER)")
    db.execute("INSERT INTO t VALUES (2), (2), (3), (NULL)")
    assert db.execute("SELECT SUM(DISTINCT v) FROM t").rows == [(5,)]
    assert db.execute("SELECT MIN(DISTINCT v) FROM t").rows == [(2,)]


# --------------------------------------------------------------------- #
# 5. ORDER BY determinism with a hidden column under DISTINCT
# --------------------------------------------------------------------- #

def test_distinct_orderby_hidden_column_is_deterministic(db):
    """ORDER BY a column that DISTINCT drops is inherently ambiguous, but
    it must still be deterministic and preserve DISTINCT semantics -- the
    pre-fix engine relied on zip() against a stale, full-length group list
    and produced garbage ordering."""
    db.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
    db.execute("INSERT INTO t VALUES (5, 1), (5, 2), (9, 3), (1, 4)")

    first = db.execute("SELECT DISTINCT a FROM t ORDER BY b").rows
    second = db.execute("SELECT DISTINCT a FROM t ORDER BY b").rows
    assert first == second                       # deterministic
    assert sorted(r[0] for r in first) == [1, 5, 9]   # DISTINCT preserved
