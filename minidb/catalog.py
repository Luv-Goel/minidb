"""System catalog: which tables exist, their columns and B+ tree roots.

The catalog lives on page 1 as a JSON document. It fits schemas of
realistic test/app sizes; exceeding the page raises DatabaseError (the
supported fix would be a catalog overflow chain).
"""

from __future__ import annotations

import json

from .errors import DatabaseError, SchemaError
from .pager import PAGE_SIZE, Pager

CATALOG_PAGE = 1


class Catalog:
    def __init__(self, pager: Pager):
        self.pager = pager
        raw = pager.read(CATALOG_PAGE)
        text = raw.split(b"\x00", 1)[0].decode("utf-8", "strict") \
            if raw.strip(b"\x00") else ""
        self.tables: dict[str, dict] = json.loads(text) if text else {}

    def _persist(self) -> None:
        blob = json.dumps(self.tables, separators=(",", ":")).encode()
        if len(blob) > PAGE_SIZE - 16:
            raise DatabaseError("catalog too large for one page")
        page = bytearray(PAGE_SIZE)
        page[:len(blob)] = blob
        self.pager.write(CATALOG_PAGE, page)

    def create_table(self, name: str, columns: list[tuple[str, str]],
                     root: int) -> None:
        if name in self.tables:
            raise SchemaError(f"table {name!r} already exists")
        if not columns:
            raise SchemaError("table must have at least one column")
        names = [c[0] for c in columns]
        if len(set(names)) != len(names):
            raise SchemaError("duplicate column name")
        self.tables[name] = {"columns": columns, "root": root, "next_rowid": 1}
        self._persist()

    def drop_table(self, name: str) -> dict:
        if name not in self.tables:
            raise SchemaError(f"no such table {name!r}")
        meta = self.tables.pop(name)
        self._persist()
        return meta

    def table(self, name: str) -> dict:
        if name not in self.tables:
            raise SchemaError(f"no such table {name!r}")
        return self.tables[name]

    def columns(self, name: str) -> list[tuple[str, str]]:
        return [tuple(c) for c in self.table(name)["columns"]]

    def set_root(self, name: str, root: int) -> None:
        self.tables[name]["root"] = root
        self._persist()

    def next_rowid(self, name: str) -> int:
        t = self.table(name)
        rid = t["next_rowid"]
        t["next_rowid"] = rid + 1
        self._persist()
        return rid
