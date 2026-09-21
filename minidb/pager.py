"""Page-oriented storage: fixed 4096-byte pages with an LRU buffer pool.

Page 0 is the meta page (magic, next unallocated page id, free list head,
whether the file is clean). Page 1 is the catalog page owned by
``catalog.py``. Everything else belongs to B+ trees.

The pool is write-back: dirty pages are kept in memory until ``flush()``
(checkpoint) writes and fsyncs them.
"""

from __future__ import annotations

import os
import struct
from collections import OrderedDict

from .errors import DatabaseError

PAGE_SIZE = 4096
MAGIC = b"MINIDB01"
META_FORMAT = "<8s I I"          # magic, next_page, free_head
META_SIZE = struct.calcsize(META_FORMAT)


class Pager:
    def __init__(self, path: str, cache_size: int = 1024):
        self.path = path
        self._cache: OrderedDict[int, bytearray] = OrderedDict()
        self._dirty: set[int] = set()
        self._cache_size = cache_size
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "r+b" if not fresh else "w+b")
        if fresh:
            self._next_page = 2      # 0 = this meta page, 1 = catalog
            self._free_head = 0
            self._write_meta()
        else:
            with open(path, "rb") as fh:
                header = fh.read(META_SIZE)
            if len(header) < META_SIZE or header[:8] != MAGIC:
                raise DatabaseError(f"{path} is not a minidb database")
            _, self._next_page, self._free_head = struct.unpack(
                META_FORMAT, header)

    # ---------------- meta ---------------- #

    def _write_meta(self) -> None:
        self._fh.seek(0)
        self._fh.write(struct.pack(META_FORMAT, MAGIC, self._next_page,
                                   self._free_head))

    @property
    def page_count(self) -> int:
        return self._next_page

    # ---------------- allocation ---------------- #

    def alloc(self) -> int:
        if self._free_head:
            pid = self._free_head
            self._free_head = struct.unpack("<I", self.read(pid)[0:4])[0]
        else:
            pid = self._next_page
            self._next_page += 1
        self._write_meta()
        return pid

    def free(self, page_id: int) -> None:
        page = self.read(page_id)
        page[:] = b"\x00" * PAGE_SIZE
        struct.pack_into("<I", page, 0, self._free_head)
        self._free_head = page_id
        self._write_meta()
        self._dirty.add(page_id)

    # ---------------- IO ---------------- #

    def read(self, page_id: int) -> bytearray:
        if page_id <= 0 or page_id >= self._next_page:
            raise DatabaseError(f"invalid page id {page_id}")
        if page_id in self._cache:
            self._cache.move_to_end(page_id)
            return self._cache[page_id]
        self._fh.seek(page_id * PAGE_SIZE)
        data = self._fh.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            data = data + b"\x00" * (PAGE_SIZE - len(data))
        page = bytearray(data)
        self._cache[page_id] = page
        self._evict()
        return page

    def write(self, page_id: int, data: bytes | bytearray) -> None:
        if len(data) != PAGE_SIZE:
            raise DatabaseError("pages are exactly 4096 bytes")
        self._cache[page_id] = bytearray(data)
        self._cache.move_to_end(page_id)
        self._dirty.add(page_id)
        self._evict()

    def mark_dirty(self, page_id: int) -> None:
        """Mark a page mutated in place through a cached reference."""
        self._dirty.add(page_id)

    def _evict(self) -> None:
        while len(self._cache) > self._cache_size:
            pid, _ = self._cache.popitem(last=False)
            if pid in self._dirty:
                self._flush_page(pid)

    def _flush_page(self, page_id: int) -> None:
        self._fh.seek(page_id * PAGE_SIZE)
        self._fh.write(self._cache[page_id])
        self._dirty.discard(page_id)

    def flush(self) -> None:
        """Checkpoint: write all dirty pages, fsync the file."""
        for pid in list(self._dirty):
            if pid in self._cache:
                self._flush_page(pid)
        self._dirty.clear()
        try:
            self._fh.flush()
        except ValueError:
            # real file-backed pager always open; defensive only
            pass
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._fh.close()
