"""B+ tree over the paged file.

- Keys are 64-bit signed integers (row ids); values are length-prefixed
  byte strings (encoded rows).
- Leaf pages are doubly linked for fast ordered scans.
- Internal pages hold separator keys; key K_i in a node is the smallest
  key reachable through child i+1 (textbook layout).
- Deletion is lazy: keys are removed from leaves, possibly-empty pages are
  kept (separators stay only as routing hints, so correctness holds; the
  trade-off is documented in the README).

Page layout::

    [0]      u8    node type: 1 = leaf, 2 = internal
    [1:3]    u16   number of entries
    [3:7]    u32   leaf: next leaf page id  |  internal: rightmost child
    [7:11]   u32   leaf: prev leaf page id  |  internal: unused
    [11:]    cells

Leaf cell:     i64 key | u16 value length | value bytes
Internal cell: i64 separator | u32 child page id
"""

from __future__ import annotations

import struct
from typing import Iterator, Optional

from .errors import DatabaseError
from .pager import PAGE_SIZE, Pager

LEAF, INTERNAL = 1, 2
HEADER = struct.Struct("<B H I I")
LEAF_CELL_FIXED = struct.Struct("<q H")      # key, value length
INTERNAL_CELL = struct.Struct("<q I")        # separator, child
MAX_VALUE = 1500                               # guarantees leaf splits work


class BPlusTree:
    def __init__(self, pager: Pager, root_page: int = 0):
        self.pager = pager
        self.root = root_page
        if root_page == 0:
            self.root = self._new_page(LEAF)

    # ---------------- node (de)serialization ---------------- #

    def _new_page(self, ntype: int) -> int:
        pid = self.pager.alloc()
        page = bytearray(PAGE_SIZE)
        HEADER.pack_into(page, 0, ntype, 0, 0, 0)
        self.pager.write(pid, page)
        return pid

    def _read_node(self, pid: int):
        page = self.pager.read(pid)
        ntype, count, slot_a, slot_b = HEADER.unpack_from(page, 0)
        if ntype == LEAF:
            keys, values = [], []
            off = HEADER.size
            for _ in range(count):
                key, vlen = LEAF_CELL_FIXED.unpack_from(page, off)
                off += LEAF_CELL_FIXED.size
                values.append(bytes(page[off:off + vlen]))
                off += vlen
                keys.append(key)
            return {"type": LEAF, "pid": pid, "keys": keys, "values": values,
                    "next": slot_a, "prev": slot_b}
        keys, children = [], []
        off = HEADER.size
        for _ in range(count):
            key, child = INTERNAL_CELL.unpack_from(page, off)
            off += INTERNAL_CELL.size
            keys.append(key)
            children.append(child)
        children.append(slot_a)  # rightmost child
        return {"type": INTERNAL, "pid": pid, "keys": keys,
                "children": children}

    def _write_node(self, node) -> None:
        page = bytearray(PAGE_SIZE)
        if node["type"] == LEAF:
            HEADER.pack_into(page, 0, LEAF, len(node["keys"]),
                             node["next"], node["prev"])
            off = HEADER.size
            for key, value in zip(node["keys"], node["values"]):
                LEAF_CELL_FIXED.pack_into(page, off, key, len(value))
                off += LEAF_CELL_FIXED.size
                page[off:off + len(value)] = value
                off += len(value)
            if off > PAGE_SIZE:
                raise DatabaseError("leaf page overflow (internal error)")
        else:
            HEADER.pack_into(page, 0, INTERNAL, len(node["keys"]),
                             node["children"][-1], 0)
            off = HEADER.size
            for key, child in zip(node["keys"], node["children"]):
                INTERNAL_CELL.pack_into(page, off, key, child)
                off += INTERNAL_CELL.size
            if off > PAGE_SIZE:
                raise DatabaseError("internal page overflow (internal error)")
        self.pager.write(node["pid"], page)

    @staticmethod
    def _leaf_fits(keys: list[int], values: list[bytes]) -> bool:
        size = HEADER.size + sum(
            LEAF_CELL_FIXED.size + len(v) for v in values)
        return size <= PAGE_SIZE

    @staticmethod
    def _internal_fits(n_keys: int) -> bool:
        return HEADER.size + n_keys * INTERNAL_CELL.size <= PAGE_SIZE

    # ---------------- point lookups ---------------- #

    def _find_leaf(self, key: int) -> dict:
        node = self._read_node(self.root)
        while node["type"] != LEAF:
            i = 0
            while i < len(node["keys"]) and key >= node["keys"][i]:
                i += 1
            node = self._read_node(node["children"][i])
        return node

    def get(self, key: int) -> Optional[bytes]:
        leaf = self._find_leaf(key)
        i = self._lower_index(leaf["keys"], key)
        if i < len(leaf["keys"]) and leaf["keys"][i] == key:
            return leaf["values"][i]
        return None

    @staticmethod
    def _lower_index(keys: list[int], key: int) -> int:
        lo, hi = 0, len(keys)
        while lo < hi:
            mid = (lo + hi) // 2
            if keys[mid] < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    # ---------------- insert ---------------- #

    def put(self, key: int, value: bytes) -> None:
        if len(value) > MAX_VALUE:
            raise DatabaseError(
                f"row too large ({len(value)} bytes > {MAX_VALUE})")
        split = self._insert(self.root, key, value)
        if split is not None:                      # root split: grow upward
            sep, right_pid = split
            new_root = self._new_page(INTERNAL)
            node = {"type": INTERNAL, "pid": new_root,
                    "keys": [sep], "children": [self.root, right_pid]}
            self._write_node(node)
            self.root = new_root

    def _insert(self, pid: int, key: int, value: bytes):
        node = self._read_node(pid)
        if node["type"] == LEAF:
            i = self._lower_index(node["keys"], key)
            if i < len(node["keys"]) and node["keys"][i] == key:
                node["values"][i] = value            # replace
            else:
                node["keys"].insert(i, key)
                node["values"].insert(i, value)
            if self._leaf_fits(node["keys"], node["values"]):
                self._write_node(node)
                return None
            return self._split_leaf(node)
        # internal: descend, then absorb a possible child split
        i = 0
        while i < len(node["keys"]) and key >= node["keys"][i]:
            i += 1
        split = self._insert(node["children"][i], key, value)
        if split is None:
            return None
        sep, right_pid = split
        node["keys"].insert(i, sep)
        node["children"].insert(i + 1, right_pid)
        if self._internal_fits(len(node["keys"])):
            self._write_node(node)
            return None
        return self._split_internal(node)

    def _split_leaf(self, node):
        n = len(node["keys"])
        half = n // 2
        right_pid = self._new_page(LEAF)
        right = {"type": LEAF, "pid": right_pid,
                 "keys": node["keys"][half:], "values": node["values"][half:],
                 "next": node["next"], "prev": node["pid"]}
        node["keys"] = node["keys"][:half]
        node["values"] = node["values"][:half]
        node["next"] = right_pid
        self._write_node(node)
        self._write_node(right)
        if right["next"]:
            nxt = self._read_node(right["next"])
            nxt["prev"] = right_pid
            self._write_node(nxt)
        return right["keys"][0], right_pid

    def _split_internal(self, node):
        n = len(node["keys"])
        mid = n // 2
        push_key = node["keys"][mid]
        right_pid = self._new_page(INTERNAL)
        right = {"type": INTERNAL, "pid": right_pid,
                 "keys": node["keys"][mid + 1:],
                 "children": node["children"][mid + 1:]}
        node["keys"] = node["keys"][:mid]
        node["children"] = node["children"][:mid + 1]
        self._write_node(node)
        self._write_node(right)
        return push_key, right_pid

    # ---------------- delete (lazy: no rebalancing) ---------------- #

    def delete(self, key: int) -> bool:
        leaf = self._find_leaf(key)
        i = self._lower_index(leaf["keys"], key)
        if i < len(leaf["keys"]) and leaf["keys"][i] == key:
            del leaf["keys"][i]
            del leaf["values"][i]
            self._write_node(leaf)
            return True
        return False

    # ---------------- ordered scans ---------------- #

    def scan(self, lo: Optional[int] = None, hi: Optional[int] = None
             ) -> Iterator[tuple[int, bytes]]:
        """Yield (key, value) for lo <= key <= hi (both optional), in order."""
        if lo is not None:
            leaf = self._find_leaf(lo)
            i = self._lower_index(leaf["keys"], lo)
        else:
            node = self._read_node(self.root)
            while node["type"] != LEAF:
                node = self._read_node(node["children"][0])
            leaf, i = node, 0
        while True:
            while i < len(leaf["keys"]):
                key = leaf["keys"][i]
                if hi is not None and key > hi:
                    return
                yield key, leaf["values"][i]
                i += 1
            if leaf["next"] == 0:
                return
            leaf = self._read_node(leaf["next"])
            i = 0

    def count(self) -> int:
        return sum(1 for _ in self.scan())

    # ---------------- teardown ---------------- #

    def drop(self) -> None:
        """Return every page owned by this tree to the free list."""
        stack = [self.root]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid == 0 or pid in seen:
                continue
            seen.add(pid)
            node = self._read_node(pid)
            if node["type"] == INTERNAL:
                stack.extend(node["children"])
        for pid in seen:
            self.pager.free(pid)
