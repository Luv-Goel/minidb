"""Query engine: statement execution, transactions, expression evaluation.

Architecture:

- tables are B+ trees keyed by an internal rowid; rows are JSON dicts
- ``execute(sql)`` parses, plans (selection push-down where free), runs
- transactions buffer undo (before-images) and redo (after-images);
  COMMIT goes through the WAL + checkpoint, ROLLBACK applies undo
- SELECT is a pull pipeline: scan -> join -> filter -> group/aggregate
  -> having -> order -> offset/limit
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import ast
from .bplustree import BPlusTree
from .catalog import Catalog
from .errors import DatabaseError, ExecutionError, SchemaError
from .pager import Pager
from .parser import parse
from .wal import WAL


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    rowcount: int
    kinds: str = "select"

    def __iter__(self):
        return iter(self.rows)


def _encode_row(row: dict) -> bytes:
    return json.dumps(row, separators=(",", ":")).encode("utf-8")


def _decode_row(blob: bytes) -> dict:
    return json.loads(blob)


class Transaction:
    def __init__(self, txid: int):
        self.txid = txid
        self.undo: list[tuple[str, int, Optional[dict]]] = []
        # redo keyed by (table,rowid); value None = delete
        self.redo: dict[tuple[str, int], Optional[dict]] = {}


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.pager = Pager(path)
        self.catalog = Catalog(self.pager)
        self.wal = WAL(path + ".wal")
        self._trees: dict[str, BPlusTree] = {}
        self.tx: Optional[Transaction] = None
        self._next_txid = 1
        self._recover()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def _recover(self) -> None:
        for op in self.wal.committed_ops():
            self._next_txid = max(self._next_txid, 1)
            table, rowid = op["table"], op["rowid"]
            if op["op"] == "put":
                self._tree(table).put(rowid, _encode_row(op["row"]))
            else:
                self._tree(table).delete(rowid)
        if os.path.getsize(self.wal.path) > 0:
            self.pager.flush()
            self.wal.truncate()

    def close(self) -> None:
        """Close cleanly: an open transaction is rolled back first so its
        pages never reach disk ahead of the WAL (no dirty-page stealing)."""
        if self.tx is not None:
            self._rollback()
        self.pager.close()

    def _simulate_crash(self) -> None:
        """Testing hook: act as if the process died — drop dirty pages,
        skip rollback, skip checkpoints. The WAL is the only survivor."""
        self.pager._dirty.clear()
        self.pager._fh.close()
        self.tx = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _tree(self, table: str) -> BPlusTree:
        if table not in self._trees:
            self._trees[table] = BPlusTree(
                self.pager, self.catalog.table(table)["root"])
        return self._trees[table]

    # ------------------------------------------------------------------ #
    # transactions
    # ------------------------------------------------------------------ #

    def _begin(self) -> None:
        if self.tx is not None:
            raise DatabaseError("transaction already active")
        self.tx = Transaction(self._next_txid)
        self._next_txid += 1

    def _commit(self) -> None:
        if self.tx is None:
            raise DatabaseError("no active transaction")
        ops = []
        for (table, rowid), row in self.tx.redo.items():
            if row is None:
                ops.append({"op": "delete", "table": table, "rowid": rowid})
            else:
                ops.append({"op": "put", "table": table,
                            "rowid": rowid, "row": row})
        self.wal.append(self.tx.txid, ops)      # 1: durable redo
        self._persist_tree_roots()
        self.pager.flush()                      # 2: checkpoint
        self.wal.truncate()                     # 3: log recycling
        self.tx = None

    def _rollback(self) -> None:
        if self.tx is None:
            raise DatabaseError("no active transaction")
        for table, rowid, before in reversed(self.tx.undo):
            tree = self._tree(table)
            if before is None:
                tree.delete(rowid)
            else:
                tree.put(rowid, _encode_row(before))
        self._persist_tree_roots()
        self.tx = None

    def _persist_tree_roots(self) -> None:
        for name, tree in self._trees.items():
            if name in self.catalog.tables:
                self.catalog.tables[name]["root"] = tree.root
        self.catalog._persist()

    def _record_write(self, table: str, rowid: int,
                      after: Optional[dict]) -> None:
        if self.tx is None:
            self._begin()
            try:
                self._apply_write(table, rowid, after)
                self._commit()
            except Exception:
                self._rollback()
                raise
        else:
            self._apply_write(table, rowid, after)

    def _apply_write(self, table: str, rowid: int,
                     after: Optional[dict]) -> None:
        assert self.tx is not None
        tree = self._tree(table)
        before_raw = tree.get(rowid)
        before = _decode_row(before_raw) if before_raw is not None else None
        self.tx.undo.append((table, rowid, before))
        if after is None:
            tree.delete(rowid)
            self.tx.redo[(table, rowid)] = None
        else:
            tree.put(rowid, _encode_row(after))
            self.tx.redo[(table, rowid)] = after

    # ------------------------------------------------------------------ #
    # statement dispatch
    # ------------------------------------------------------------------ #

    def execute(self, sql: str) -> Result:
        stmt = parse(sql)
        if isinstance(stmt, ast.Explain):
            import pprint
            plan = pprint.pformat(stmt.stmt)
            return Result(["plan"], [(line,) for line in plan.splitlines()], len(plan.splitlines()), "explain")
        if isinstance(stmt, ast.Select):
            return self._select(stmt)
        if isinstance(stmt, ast.Insert):
            return self._insert(stmt)
        if isinstance(stmt, ast.Update):
            return self._update(stmt)
        if isinstance(stmt, ast.Delete):
            return self._delete(stmt)
        if isinstance(stmt, ast.CreateTable):
            return self._create(stmt)
        if isinstance(stmt, ast.DropTable):
            return self._drop(stmt)
        if isinstance(stmt, ast.Begin):
            self._begin()
            return Result([], [], 0, "begin")
        if isinstance(stmt, ast.Commit):
            self._commit()
            return Result([], [], 0, "commit")
        if isinstance(stmt, ast.Rollback):
            self._rollback()
            return Result([], [], 0, "rollback")
        raise DatabaseError("unsupported statement")

    # ------------------------------------------------------------------ #
    # DDL
    # ------------------------------------------------------------------ #

    def _create(self, stmt: ast.CreateTable) -> Result:
        if stmt.table in self.catalog.tables:
            if stmt.if_not_exists:
                return Result([], [], 0, "ddl")
            raise SchemaError(f"table {stmt.table!r} already exists")
        tree = BPlusTree(self.pager)
        self.catalog.create_table(stmt.table, stmt.columns, tree.root)
        self._trees[stmt.table] = tree
        self.pager.flush()                    # DDL is auto-committed
        return Result([], [], 0, "ddl")

    def _drop(self, stmt: ast.DropTable) -> Result:
        if stmt.table not in self.catalog.tables:
            if stmt.if_exists:
                return Result([], [], 0, "ddl")
            raise SchemaError(f"no such table {stmt.table!r}")
        meta = self.catalog.drop_table(stmt.table)
        BPlusTree(self.pager, meta["root"]).drop()
        self._trees.pop(stmt.table, None)
        self.pager.flush()
        return Result([], [], 0, "ddl")

    # ------------------------------------------------------------------ #
    # DML
    # ------------------------------------------------------------------ #

    def _validate_row(self, table: str, row: dict) -> dict:
        cols = self.catalog.columns(table)
        out = {}
        for name, ctype in cols:
            value = row.get(name)
            if value is not None:
                if ctype == "INTEGER" and not isinstance(value, int):
                    raise ExecutionError(
                        f"column {name!r} expects INTEGER, got {value!r}")
                if ctype == "TEXT" and not isinstance(value, str):
                    raise ExecutionError(
                        f"column {name!r} expects TEXT, got {value!r}")
            out[name] = value
        return out

    def _insert(self, stmt: ast.Insert) -> Result:
        self.catalog.table(stmt.table)         # existence check
        col_names = [c for c, _ in self.catalog.columns(stmt.table)]
        count = 0
        for exprs in stmt.rows:
            if stmt.columns is not None:
                if len(exprs) != len(stmt.columns):
                    raise ExecutionError("column count does not match "
                                         "value count")
                names = stmt.columns
                for n in names:
                    if n not in col_names:
                        raise SchemaError(
                            f"table {stmt.table!r} has no column {n!r}")
            else:
                if len(exprs) != len(col_names):
                    raise ExecutionError(
                        f"table {stmt.table!r} has {len(col_names)} columns "
                        f"but {len(exprs)} values were supplied")
                names = col_names
            values = [self._eval(e, {}, {}) for e in exprs]
            row = self._validate_row(stmt.table, dict(zip(names, values)))
            rowid = self.catalog.next_rowid(stmt.table)
            self._record_write(stmt.table, rowid, row)
            count += 1
        return Result([], [], count, "insert")

    def _update(self, stmt: ast.Update) -> Result:
        col_names = {c for c, _ in self.catalog.columns(stmt.table)}
        for name, _ in stmt.assignments:
            if name not in col_names:
                raise SchemaError(
                    f"table {stmt.table!r} has no column {name!r}")
        targets = []
        for rowid, row in self._scan_rows(stmt.table):
            env = self._row_env(stmt.table, rowid, row)
            if stmt.where is None or self._truthy(
                    self._eval(stmt.where, env, {})):
                targets.append((rowid, row))
        for rowid, row in targets:
            env = self._row_env(stmt.table, rowid, row)
            new_row = dict(row)
            for name, expr in stmt.assignments:
                new_row[name] = self._eval(expr, env, {})
            new_row = self._validate_row(stmt.table, new_row)
            self._record_write(stmt.table, rowid, new_row)
        return Result([], [], len(targets), "update")

    def _delete(self, stmt: ast.Delete) -> Result:
        targets = []
        for rowid, row in self._scan_rows(stmt.table):
            env = self._row_env(stmt.table, rowid, row)
            if stmt.where is None or self._truthy(
                    self._eval(stmt.where, env, {})):
                targets.append(rowid)
        for rowid in targets:
            self._record_write(stmt.table, rowid, None)
        return Result([], [], len(targets), "delete")

    # ------------------------------------------------------------------ #
    # SELECT pipeline
    # ------------------------------------------------------------------ #

    def _scan_rows(self, table: str) -> Iterable[tuple[int, dict]]:
        for rowid, blob in list(self._tree(table).scan()):
            yield rowid, _decode_row(blob)

    def _row_env(self, source: str, rowid: int, row: dict) -> dict:
        env = {f"{source}.{k}": v for k, v in row.items()}
        env.update(row)                        # unqualified names
        env[f"{source}.rowid"] = rowid
        env["rowid"] = rowid
        return env

    def _select(self, stmt: ast.Select) -> Result:
        base = stmt.from_table
        self.catalog.table(base.name)

        # 1. FROM + JOINs (nested-loop inner joins)
        joined: list[dict] = [
            self._row_env(base.effective, rowid, row)
            for rowid, row in self._scan_rows(base.name)
        ]
        for join in stmt.joins:
            jt = join.table
            self.catalog.table(jt.name)
            jrows = [self._row_env(jt.effective, rowid, row)
                     for rowid, row in self._scan_rows(jt.name)]
            out = []
            for left in joined:
                for right in jrows:
                    merged = {**left, **right}
                    # keep unqualified names from the *left* table visible
                    for k, v in left.items():
                        merged.setdefault(k, v)
                    if self._truthy(self._eval(join.on, merged, {})):
                        out.append(merged)
            joined = out

        # 2. WHERE
        if stmt.where is not None:
            joined = [env for env in joined
                      if self._truthy(self._eval(stmt.where, env, {}))]

        # 3. grouping / aggregation
        has_agg = any(self._expr_has_agg(item.expr) for item in stmt.columns) \
            or (stmt.having is not None and self._expr_has_agg(stmt.having))
        groups: list[tuple[dict, list[dict]]]  # (key_env, member envs)
        if stmt.group_by or has_agg:
            buckets: dict[tuple, list[dict]] = {}
            for env in joined:
                key = tuple(self._eval(e, env, {}) for e in stmt.group_by)
                buckets.setdefault(key, []).append(env)
            if not buckets and not stmt.group_by:
                buckets[()] = []               # aggregate over empty set
            groups = [({}, envs) for envs in buckets.values()]
        else:
            groups = [(env, [env]) for env in joined]

        # 4. HAVING
        if stmt.having is not None:
            groups = [(env, members) for env, members in groups
                      if self._truthy(self._eval_group(
                          stmt.having, env, members))]

        # 5. projection: `*` expands to base table cols (+ joined tables')
        out_cols: list[str] = []
        for item in stmt.columns:
            if isinstance(item.expr, ast.Column) and item.expr.name == "*":
                out_cols.extend(self._star_columns(stmt))
            else:
                out_cols.append(item.alias or ast.sql_repr(item.expr))
        rows: list[tuple] = []
        for env, members in groups:
            row_out: list[Any] = []
            for item in stmt.columns:
                if isinstance(item.expr, ast.Column) and item.expr.name == "*":
                    base = members[0] if members else env
                    for source, col in self._star_column_sources(stmt):
                        row_out.append(base.get(f"{source}.{col}"))
                else:
                    row_out.append(self._eval_group(item.expr, env, members))
            rows.append(tuple(row_out))

        # 6. DISTINCT
        if stmt.distinct:
            seen = set()
            unique = []
            for r in rows:
                if r not in seen:
                    seen.add(r)
                    unique.append(r)
            rows = unique

        # 7. ORDER BY — evaluated against the group envs / output hybrids
        if stmt.order_by:
            decorated = []
            for r, (env, members) in zip(rows, groups):
                key = tuple(
                    self._eval_group(e, {**env, **{
                        out_cols[i]: r[i] for i in range(len(out_cols))}},
                        members)
                    for e, _ in stmt.order_by)
                decorated.append((key, r))
            for pos in reversed(range(len(stmt.order_by))):
                direction = stmt.order_by[pos][1]
                decorated.sort(key=_SortKey(pos), reverse=direction == "desc")
            rows = [r for _, r in decorated]

        # 8. LIMIT / OFFSET
        rows = rows[stmt.offset:]
        if stmt.limit is not None:
            rows = rows[:stmt.limit]

        return Result(out_cols, rows, len(rows), "select")

    def _eval_group(self, expr: ast.Expr, env: dict,
                    members: list[dict]) -> Any:
        """Evaluate against a group: aggregate fns range over members."""
        if isinstance(expr, ast.FuncCall) and expr.name.lower() in \
                ast.AGGREGATES:
            return self._aggregate(expr, env, members)
        if isinstance(expr, ast.BinOp):
            f = BINOPS[expr.op]
            return f(self._eval_group(expr.left, env, members),
                     self._eval_group(expr.right, env, members))
        if isinstance(expr, ast.And):
            return (self._truthy(self._eval_group(expr.left, env, members))
                    and self._truthy(self._eval_group(expr.right, env,
                                                      members)))
        if isinstance(expr, ast.Or):
            return (self._truthy(self._eval_group(expr.left, env, members))
                    or self._truthy(self._eval_group(expr.right, env,
                                                     members)))
        if isinstance(expr, ast.Not):
            return not self._truthy(self._eval_group(expr.operand, env,
                                                     members))
        if isinstance(expr, (ast.IsNull, ast.Like, ast.In, ast.Between)):
            base_env = members[0] if members else env
            return self._eval(expr, {**base_env, **env}, {"members": members})
        base_env = members[0] if members else env
        return self._eval(expr, {**base_env, **env}, {})

    def _star_column_sources(self, stmt: ast.Select) -> list[tuple[str, str]]:
        """(source_alias, column) pairs a bare `*` expands to."""
        pairs = [(stmt.from_table.effective, c)
                 for c, _ in self.catalog.columns(stmt.from_table.name)]
        for join in stmt.joins:
            pairs.extend((join.table.effective, c)
                         for c, _ in self.catalog.columns(join.table.name))
        return pairs

    def _star_columns(self, stmt: ast.Select) -> list[str]:
        return [c for _, c in self._star_column_sources(stmt)]

    def _aggregate(self, fn: ast.FuncCall, env: dict,
                   members: list[dict]) -> Any:
        name = fn.name.lower()
        args = fn.args
        if name == "count":
            if not args or (isinstance(args[0], ast.Column)
                            and args[0].name == "*"):
                return len(members)
            return sum(1 for m in members
                       if self._eval(args[0], m, {}) is not None)
        values = [self._eval(args[0], m, {}) for m in members]
        values = [v for v in values if v is not None]
        if not values:
            return None
        if name == "sum":
            return sum(values)
        if name == "avg":
            return sum(values) / len(values)
        if name == "min":
            return min(values)
        if name == "max":
            return max(values)
        raise ExecutionError(f"unknown aggregate {name}")

    @staticmethod
    def _expr_has_agg(node: ast.Expr) -> bool:
        if isinstance(node, ast.FuncCall):
            return node.name.lower() in ast.AGGREGATES
        for attr in ("left", "right", "operand", "options"):
            sub = getattr(node, attr, None)
            if isinstance(sub, ast.Expr) and Database._expr_has_agg(sub):
                return True
            if isinstance(sub, list) and any(
                    isinstance(x, ast.Expr) and Database._expr_has_agg(x)
                    for x in sub):
                return True
        if isinstance(node, ast.FuncCall):
            return any(Database._expr_has_agg(a) for a in node.args)
        return False

    # ------------------------------------------------------------------ #
    # scalar expression evaluation
    # ------------------------------------------------------------------ #

    def _eval(self, expr: ast.Expr, env: dict, ctx: dict) -> Any:
        if isinstance(expr, ast.Literal):
            return expr.value
        if isinstance(expr, ast.Column):
            key = f"{expr.table}.{expr.name}" if expr.table else expr.name
            if key in env:
                return env[key]
            if expr.table:
                raise ExecutionError(f"no such column {key!r}")
            raise ExecutionError(f"no such column {expr.name!r}")
        if isinstance(expr, ast.BinOp):
            return BINOPS[expr.op](self._eval(expr.left, env, ctx),
                                   self._eval(expr.right, env, ctx))
        if isinstance(expr, ast.And):
            return self._truthy(self._eval(expr.left, env, ctx)) and \
                self._truthy(self._eval(expr.right, env, ctx))
        if isinstance(expr, ast.Or):
            return self._truthy(self._eval(expr.left, env, ctx)) or \
                self._truthy(self._eval(expr.right, env, ctx))
        if isinstance(expr, ast.Not):
            return not self._truthy(self._eval(expr.operand, env, ctx))
        if isinstance(expr, ast.IsNull):
            result = self._eval(expr.operand, env, ctx) is None
            return not result if expr.negated else result
        if isinstance(expr, ast.Like):
            value = self._eval(expr.operand, env, ctx)
            pattern = self._eval(expr.pattern, env, ctx)
            if value is None or pattern is None:
                return False
            regex = "^" + re.escape(str(pattern)).replace(
                r"%", ".*").replace(r"_", ".") + "$"
            matched = re.match(regex, str(value)) is not None
            return not matched if expr.negated else matched
        if isinstance(expr, ast.In):
            value = self._eval(expr.operand, env, ctx)
            options = [self._eval(o, env, ctx) for o in expr.options]
            found = value in options
            return not found if expr.negated else found
        if isinstance(expr, ast.Between):
            v = self._eval(expr.operand, env, ctx)
            lo = self._eval(expr.low, env, ctx)
            hi = self._eval(expr.high, env, ctx)
            if v is None or lo is None or hi is None:
                return False
            inside = lo <= v <= hi
            return not inside if expr.negated else inside
        if isinstance(expr, ast.FuncCall):
            name = expr.name.lower()
            if name in ast.AGGREGATES:
                members = (ctx or {}).get("members", [env])
                return self._aggregate(expr, env, members)
            vals = [self._eval(a, env, ctx) for a in expr.args]
            return _scalar_fn(name, vals)
        raise ExecutionError(f"cannot evaluate {expr!r}")

    @staticmethod
    def _truthy(value: Any) -> bool:
        return value is not None and value is not False and value != 0 \
            and value != ""


def _arith(op):
    def fn(a, b):
        if a is None or b is None:
            return None
        return op(a, b)
    return fn


def _cmp(op):
    def fn(a, b):
        if a is None or b is None:
            return None                    # NULL semantics: unknown
        return op(a, b)
    return fn


import operator
BINOPS = {
    "+": _arith(operator.add), "-": _arith(operator.sub),
    "*": _arith(operator.mul), "/": _arith(operator.truediv),
    "%": _arith(operator.mod),
    "||": _arith(lambda a, b: str(a) + str(b)),
    "=": _cmp(operator.eq), "!=": _cmp(operator.ne),
    "<": _cmp(operator.lt), "<=": _cmp(operator.le),
    ">": _cmp(operator.gt), ">=": _cmp(operator.ge),
}


class _SortKey:
    """Sort on tuple position i with NULLs last and mixed types ordered."""

    def __init__(self, pos: int):
        self.pos = pos

    def __call__(self, item):
        v = item[0][self.pos]
        rank = 2 if v is None else (0 if isinstance(v, (int, float)) else 1)
        return (rank, v if rank != 2 else 0 if rank == 0 else "")


def _scalar_fn(name: str, args: list) -> Any:
    if name == "length":
        return len(str(args[0])) if args[0] is not None else None
    if name == "upper":
        return str(args[0]).upper() if args[0] is not None else None
    if name == "lower":
        return str(args[0]).lower() if args[0] is not None else None
    if name == "abs":
        return abs(args[0]) if args[0] is not None else None
    if name == "coalesce":
        return next((a for a in args if a is not None), None)
    if name == "round":
        if args[0] is None: return None
        ndigits = int(args[1]) if len(args) > 1 and args[1] is not None else 0
        return round(float(args[0]), ndigits)
    if name == "trim":
        return str(args[0]).strip() if args[0] is not None else None
    if name == "ltrim":
        return str(args[0]).lstrip() if args[0] is not None else None
    if name == "rtrim":
        return str(args[0]).rstrip() if args[0] is not None else None
    if name == "concat":
        return "".join(str(a) for a in args if a is not None)
    import random
    if name == "random":
        return random.randint(-9223372036854775808, 9223372036854775807)
    raise ExecutionError(f"unknown function {name!r}")
