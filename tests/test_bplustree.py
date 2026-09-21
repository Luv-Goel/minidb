"""B+ tree unit tests: splits, ordering, scans, structural persistence."""

import tempfile
import unittest
from pathlib import Path

from minidb.bplustree import BPlusTree
from minidb.pager import Pager


class BPlusTreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "t.db")
        self.pager = Pager(self.path)
        self.tree = BPlusTree(self.pager)

    def tearDown(self):
        self.pager.close()
        self.tmp.cleanup()

    def _populate(self, n):
        for i in range(n):
            self.tree.put(i, f"row-{i}".encode())

    def test_put_get(self):
        self.tree.put(1, b"one")
        self.tree.put(2, b"two")
        self.assertEqual(self.tree.get(1), b"one")
        self.assertEqual(self.tree.get(2), b"two")
        self.assertIsNone(self.tree.get(3))

    def test_overwrite(self):
        self.tree.put(7, b"old")
        self.tree.put(7, b"new")
        self.assertEqual(self.tree.get(7), b"new")
        self.assertEqual(self.tree.count(), 1)

    def test_mass_insert_balanced(self):
        self._populate(2000)                    # forces multiple split levels
        self.assertEqual(self.tree.count(), 2000)
        self.assertEqual(self.tree.get(0), b"row-0")
        self.assertEqual(self.tree.get(1999), b"row-1999")

    def test_random_order_insert(self):
        import random
        keys = list(range(500))
        random.Random(42).shuffle(keys)
        for k in keys:
            self.tree.put(k, f"v{k}".encode())
        self.assertEqual([k for k, _ in self.tree.scan()], list(range(500)))

    def test_scan_range(self):
        self._populate(1000)
        got = [k for k, _ in self.tree.scan(lo=100, hi=110)]
        self.assertEqual(got, list(range(100, 111)))

    def test_delete(self):
        self._populate(300)
        for k in range(0, 300, 2):
            self.assertTrue(self.tree.delete(k))
        self.assertFalse(self.tree.delete(0))
        self.assertEqual([k for k, _ in self.tree.scan()],
                         list(range(1, 300, 2)))

    def test_persistence_across_reopen(self):
        self._populate(400)
        root = self.tree.root
        self.pager.flush()
        self.pager.close()
        self.pager = Pager(self.path)
        tree2 = BPlusTree(self.pager, root)
        self.assertEqual(tree2.count(), 400)
        self.assertEqual(tree2.get(250), b"row-250")

    def test_value_too_large_rejected(self):
        from minidb.errors import DatabaseError
        with self.assertRaises(DatabaseError):
            self.tree.put(1, b"x" * 10_000)


if __name__ == "__main__":
    unittest.main()
