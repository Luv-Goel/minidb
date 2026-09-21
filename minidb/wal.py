"""Write-ahead log: durability + crash recovery.

Protocol
--------
1. Mutations inside a transaction buffer *redo* records in memory.
2. COMMIT: append all records + a commit marker, fsync, THEN flush pages
   and truncate the log.
3. On open, a non-empty WAL means an unclean shutdown (or no checkpoint
   yet): replay the records of transactions that reached their commit
   marker — idempotent, since records carry absolute after-images.
4. ROLLBACK never touches the WAL; in-memory undo restores the old state.

Record format: one JSON object per line.
"""

from __future__ import annotations

import json
import os
from typing import Iterator


class WAL:
    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(path):
            open(path, "w").close()

    def append(self, txid: int, ops: list[dict]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            for op in ops:
                rec = {"tx": txid, **op}
                fh.write(json.dumps(rec) + "\n")
            fh.write(json.dumps({"tx": txid, "commit": True}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def committed_ops(self) -> Iterator[dict]:
        """Yield ops of committed transactions in log order."""
        pending: dict[int, list[dict]] = {}
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                tx = rec.pop("tx")
                if rec.pop("commit", False):
                    for op in pending.pop(tx, []):
                        yield op
                else:
                    pending.setdefault(tx, []).append(rec)

    def truncate(self) -> None:
        with open(self.path, "w"):
            pass  # truncate in place, atomically enough after fsync'd pages
