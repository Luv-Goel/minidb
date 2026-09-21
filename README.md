# minidb

> A small but **real** SQL database engine: pager + B+ tree storage,
> write-ahead logging, transactions, and a hand-written SQL frontend.
> Pure Python, zero dependencies.

[![CI](https://github.com/Luv-Goel/minidb/actions/workflows/ci.yml/badge.svg)](https://github.com/Luv-Goel/minidb/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

Not a wrapper around SQLite — the storage engine, the B+ balancer, the WAL
recovery, the parser, the planner and the executor are all in this repo,
totalling a couple thousand lines of readable Python.

## Feature matrix

| Layer | What works |
|---|---|
| **Storage** | 4 KiB paged file, LRU buffer pool with dirty-page tracking, free-page list |
| **Indices** | B+ tree per table (rowid-keyed): splits, linked leaves, lazy deletes |
| **Durability** | redo WAL, fsync-on-commit, checkpoint, **crash recovery replay** |
| **Transactions** | `BEGIN` / `COMMIT` / `ROLLBACK` with in-memory undo |
| **DDL** | `CREATE TABLE` / `DROP TABLE` (`IF [NOT] EXISTS`), typed columns |
| **DML** | multi-row `INSERT`, `UPDATE`, `DELETE` with arbitrary `WHERE` |
| **Queries** | `JOIN ... ON`, `WHERE` (AND/OR/NOT, `IN`, `BETWEEN`, `LIKE`, `IS NULL`), `GROUP BY` + `HAVING`, aggregates (`COUNT/SUM/AVG/MIN/MAX`), `ORDER BY`, `LIMIT`/`OFFSET`, `DISTINCT`, scalar fns (`upper/lower/length/abs/coalesce`), aliases |
| **Frontend** | tokenizer + recursive-descent parser with position-marked errors |
| **Shell** | interactive `python -m minidb.repl` with ASCII tables |

## Try it

```bash
git clone https://github.com/Luv-Goel/minidb.git
cd minidb
python -m minidb.repl demo.db      # interactive shell, stdlib only
```

```
minidb> CREATE TABLE users (id INTEGER, name TEXT, age INTEGER);
ok, 0 row(s)
minidb> INSERT INTO users VALUES (1, 'ada', 36), (2, 'grace', 85);
ok, 2 row(s)
minidb> SELECT name FROM users WHERE age >= 50;
name
-----
grace
1 row(s)
```

…or run the scripted tour:

```bash
python examples/demo.py
```

```
minidb> SELECT users.name, SUM(orders.amount) AS total
        FROM users JOIN orders ON orders.user_id = users.id
        GROUP BY users.name ORDER BY total DESC LIMIT 3
users.name | total
-----------+------
guido      | 200
ada        | 120
linus      | 10
3 row(s)
```

## Library use

```python
from minidb import Database

with Database("shop.db") as db:
    db.execute("CREATE TABLE items (sku INTEGER, label TEXT, cents INTEGER)")
    db.execute("INSERT INTO items VALUES (1, 'espresso', 350)")

    db.execute("BEGIN")
    db.execute("UPDATE items SET cents = cents + 50 WHERE sku = 1")
    db.execute("ROLLBACK")                       # never happened

    r = db.execute("SELECT label FROM items WHERE cents < 400")
    print(r.rows)                                # [('espresso',)]
```

## How the pieces fit

```
        ┌─────────┐   SQL    ┌────────┐  AST   ┌──────────┐
        │  lexer   ├────────▶│ parser ├───────▶│ executor │  engine.py
        └─────────┘          └────────┘        └────┬─────┘
                                                    │ row ops (rowid, dict)
                                             ┌──────▼──────┐
                                             │  B+ tree    │  bplustree.py
                                             └──────┬──────┘
               commit: redo records ──┐            │ page get/put
                ┌────────────┐   fsync│    ┌───────▼───────┐
                │    WAL     │◀───────┴────▶   pager      │  pager.py
                └────────────┘             └───────┬───────┘
                        ▲ replay on open           │ flush + fsync
                        │                  ┌───────▼───────┐
                        └──────────────────│  data file    │
                                           └───────────────┘
```

**Commit path (durability):** append redo records + commit marker → fsync
WAL → flush dirty pages → truncate WAL. On open, any non-empty WAL is
replayed — committed transactions re-applied, uncommitted tails dropped;
records carry absolute after-images, so replay is idempotent.

**Rollback path:** in-memory before-images applied in reverse; the WAL is
never touched for aborted work.

**B+ tree note:** deletes are *lazy* (no rebalancing — stale separators
only act as routing hints, so searches stay correct). This is the same
trade real engines like InnoDB make at page level, and it's documented
rather than hidden.

**Deliberate limits** (each has a documented fix path): rows ≤ ~1.5 KB,
catalog fits one page, secondary indexes not yet built, DDL is
auto-committed rather than transactional.

## Tests

48 tests — unit (lexer/parser/B+ tree), query semantics, transactions,
joins, and **simulated power-loss** recovery (dirty pages discarded, only
the WAL survives):

```bash
python -m unittest discover -s tests -v
```

## Layout

```
minidb/
├── minidb/
│   ├── lexer.py      # SQL tokenizer with comments and '' escapes
│   ├── parser.py     # recursive descent → typed AST
│   ├── ast.py        # statements, expressions, precedence
│   ├── pager.py      # 4 KiB pages, LRU buffer pool, free list
│   ├── bplustree.py  # balanced B+ tree with leaf links
│   ├── catalog.py    # schema registry on page 1
│   ├── wal.py        # write-ahead log + crash replay
│   ├── engine.py     # executor, transactions, aggregation, joins
│   └── repl.py       # interactive shell
├── tests/            # 48 tests, unittest, zero dependencies
└── examples/demo.py
```

## License

MIT — see [LICENSE](LICENSE).
