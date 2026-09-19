"""Disposable disk-backed batches. Paths are DPAPI-protected on Windows.

SQLite's empty filename creates a private temporary database removed on close.
Only counts and indices are unencrypted; no filename search index is persisted.
"""
import itertools
import json
import sqlite3
import time

from ..state_keys import protector


class ResultStore:
    batch_size = 128
    def __init__(self):
        self.db = sqlite3.connect("")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("CREATE TABLE batches (id INTEGER PRIMARY KEY, first INTEGER, size INTEGER, data BLOB)")
        self.db.execute("CREATE INDEX batch_position ON batches(first)")
        self.db.execute("CREATE TABLE matches (position INTEGER PRIMARY KEY, batch INTEGER, offset INTEGER)")
        self.provider = protector()
        self.count = self.batches = 0
        self.filter_key = None
        self.filtered_batches = self.matches = 0

    def __len__(self):
        return self.count

    def extend(self, rows):
        source = iter(rows)
        batches, count = self.batches, self.count
        with self.db:
            while chunk := list(itertools.islice(source, self.batch_size)):
                payload = self.provider.protect(json.dumps(chunk, ensure_ascii=True).encode("utf-8"))
                self.db.execute("INSERT INTO batches VALUES (?,?,?,?)",
                                (batches, count, len(chunk), payload))
                batches += 1
                count += len(chunk)
        self.batches, self.count = batches, count

    def decode(self, payload):
        return json.loads(self.provider.unprotect(payload))

    def __iter__(self):
        for (payload,) in self.db.execute("SELECT data FROM batches ORDER BY id"):
            yield from self.decode(payload)

    def page(self, number, size, query="", kind=0, categories=(), predicate=None, filter_token=None):
        query = query.casefold().strip()
        key = (query, kind, tuple(sorted(categories)), filter_token)
        filtered = bool(query or kind or categories or predicate)
        if filtered:
            if key != self.filter_key:
                self.db.execute("DELETE FROM matches")
                self.filter_key = key
                self.filtered_batches = self.matches = 0
            deadline = time.monotonic() + 0.03
            # Incremental work keeps changing filters and live refresh bounded.
            with self.db:
                for batch_id, payload in self.db.execute(
                        "SELECT id,data FROM batches WHERE id>=? ORDER BY id LIMIT 32", (self.filtered_batches,)):
                    for offset, row in enumerate(self.decode(payload)):
                        if predicate is not None and not predicate(row):
                            continue
                        if kind == 1 and row["is_dir"] or kind == 2 and not row["is_dir"]:
                            continue
                        if categories and not any(s["category"] in categories for s in row["signals"]):
                            continue
                        if query and query not in row["path"].casefold() and not any(
                                query in s["surface"].casefold() for s in row["signals"]):
                            continue
                        self.db.execute("INSERT INTO matches VALUES (?,?,?)", (self.matches, batch_id, offset))
                        self.matches += 1
                    self.filtered_batches = batch_id + 1
                    if time.monotonic() >= deadline:
                        break
            count = self.matches
            pending = self.filtered_batches < self.batches
        else:
            count, pending = self.count, False
        number = max(0, min(number, (count - 1) // size))
        start, stop = number * size, (number + 1) * size
        result = []
        if filtered:
            last_batch, decoded = None, None
            for batch_id, offset in self.db.execute(
                    "SELECT batch,offset FROM matches WHERE position>=? AND position<? ORDER BY position", (start, stop)):
                if batch_id != last_batch:
                    decoded = self.decode(self.db.execute("SELECT data FROM batches WHERE id=?", (batch_id,)).fetchone()[0])
                    last_batch = batch_id
                result.append(decoded[offset])
        elif count:
            first = self.db.execute("SELECT id FROM batches WHERE first<=? ORDER BY first DESC LIMIT 1", (start,)).fetchone()[0]
            cursor = self.db.execute("SELECT first,data FROM batches WHERE id>=? ORDER BY id", (first,))
            for position, payload in cursor:
                result.extend(self.decode(payload)[max(0, start-position):stop-position])
                if len(result) >= min(size, count-start):
                    break
            cursor.close()
        return result, count, pending, number

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None


class DirectoryStack:
    """LIFO traversal with at most 128 pending directories in Python memory."""
    def __init__(self):
        self.store = ResultStore()
        self.buffer = []

    def append(self, value):
        if len(self.buffer) == 128:
            try:
                self.store.extend(self.buffer)
            except Exception as exc:
                # Do not let the traversal's source-access OSError handler
                # misreport a spool failure as an unreadable source directory.
                raise RuntimeError("pending directory storage failed") from exc
            self.buffer = []
        self.buffer.append(value)

    def __bool__(self):
        return bool(self.buffer or self.store.batches)

    def pop(self):
        if not self.buffer:
            batch_id = self.store.batches - 1
            data = self.store.db.execute("SELECT data FROM batches WHERE id=?", (batch_id,)).fetchone()[0]
            self.buffer = self.store.decode(data)
            with self.store.db:
                self.store.db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            self.store.batches -= 1
            self.store.count -= len(self.buffer)
        return self.buffer.pop()

    def close(self):
        self.store.close()
