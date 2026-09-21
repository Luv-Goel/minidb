"""Interactive SQL shell.

    python -m minidb.repl mydb.sqlite   # or: minidb mydb.sqlite (console script)
"""

from __future__ import annotations

import sys

from .engine import Database
from .errors import DatabaseError

BANNER = "minidb 1.0 — a tiny SQL engine.  .help for help, .quit to exit"


def format_table(columns: list[str], rows: list[tuple]) -> str:
    if not columns:
        return "(no columns)"
    widths = [len(c) for c in columns]
    str_rows = [["" if v is None else str(v) for v in r] for r in rows]
    for r in str_rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    line = "-+-".join("-" * w for w in widths)
    out = [" | ".join(c.ljust(w) for c, w in zip(columns, widths)), line]
    for r in str_rows:
        out.append(" | ".join(cell.ljust(w) for cell, w in zip(r, widths)))
    return "\n".join(out)


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else ":memory-ish"
    if path == ":memory-ish":
        import tempfile
        import os
        path = os.path.join(tempfile.mkdtemp(), "repl.db")
        print("(using a scratch database — pass a filename to persist)")
    db = Database(path)
    print(BANNER)
    buffer = ""
    try:
        while True:
            try:
                prompt = "minidb> " if not buffer else "   ...> "
                line = input(prompt)
            except EOFError:
                print()
                break
            stripped = line.strip()
            if not buffer and stripped.startswith("."):
                if stripped in (".quit", ".exit"):
                    break
                if stripped == ".help":
                    print("  .tables          list tables")
                    print("  .schema <table>  show CREATE columns")
                    print("  SQL statements end with ';'")
                    continue
                if stripped == ".tables":
                    print("\n".join(sorted(db.catalog.tables)) or "(no tables)")
                    continue
                if stripped.startswith(".schema"):
                    name = stripped.split(maxsplit=1)[1] if " " in stripped else ""
                    if name in db.catalog.tables:
                        cols = db.catalog.columns(name)
                        print(f"CREATE TABLE {name} ("
                              + ", ".join(f"{c} {t}" for c, t in cols) + ")")
                    else:
                        print(f"no such table: {name!r}")
                    continue
                print(f"unknown command {stripped!r}; try .help")
                continue
            buffer = (buffer + " " + line).strip()
            if not buffer.rstrip().endswith(";"):
                continue
            try:
                result = db.execute(buffer.rstrip(";").strip() + "")
            except DatabaseError as e:
                print(f"error: {e}")
                buffer = ""
                continue
            buffer = ""
            if result.kinds == "select":
                print(format_table(result.columns, result.rows))
                print(f"{len(result.rows)} row(s)")
            else:
                print(f"ok, {result.rowcount} row(s) affected")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
