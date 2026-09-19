"""Disk-backed directory browser with encrypted names and bounded pages."""
import hashlib
import itertools
import bisect
import os
from ..gui.results import ResultStore


class TreeStore(ResultStore):
    batch_size = 512
    def __init__(self):
        super().__init__()
        self.key = os.urandom(32)
        self.db.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, parent BLOB, batch INTEGER, offset INTEGER, verdict BLOB)")
        self.db.execute("CREATE INDEX children ON nodes(parent,id)")

    def digest(self, path):
        return hashlib.blake2b(path.encode('utf-8', 'surrogatepass'), key=self.key).digest()

    def extend(self, rows):
        source = iter(rows)
        while chunk := list(itertools.islice(source, self.batch_size)):
            batch = self.batches
            super().extend(chunk)
            with self.db:
                self.db.executemany("INSERT INTO nodes VALUES (?,?,?,?,NULL)",
                    ((int(row['entry_id'][1:]), self.digest(row['relative_path'].rpartition('/')[0] or '.'), batch, offset)
                     for offset, row in enumerate(chunk)))

    def children(self, parent='.', after=0, size=80):
        rows = []
        last_batch, decoded = None, None
        for identifier, batch, offset, verdict in self.db.execute(
                "SELECT id,batch,offset,verdict FROM nodes WHERE parent=? AND id>? ORDER BY id LIMIT ?",
                (self.digest(parent), after, size)):
            if batch != last_batch:
                decoded = self.decode(self.db.execute("SELECT data FROM batches WHERE id=?", (batch,)).fetchone()[0])
                last_batch = batch
            row = decoded[offset]
            row['verdict'] = self.decode(verdict) if verdict else None
            rows.append(row)
        return rows

    def mark(self, decisions):
        import json
        with self.db:
            for identifier, verdict in decisions.items():
                data = self.provider.protect(json.dumps(verdict, ensure_ascii=True).encode())
                cursor = self.db.execute("UPDATE nodes SET verdict=? WHERE id=?", (data, int(identifier[1:])))
                if cursor.rowcount != 1:
                    raise ValueError('unknown inventory ID')

    def previous(self, parent, after, size=80):
        identifiers = [row[0] for row in self.db.execute(
            "SELECT id FROM nodes WHERE parent=? AND id<=? ORDER BY id DESC LIMIT ?",
            (self.digest(parent), after, size+1))]
        return identifiers[-1] if len(identifiers) > size else 0


class PreviewTreeStore:
    """Keep browse-only metadata in memory; spill new rows, never truncate.

    The 512 MiB estimate is a buffer budget, not a process RSS limit. Candidate
    reviews and saved sessions continue to use their protected stores.
    """
    def __init__(self, memory_budget=512*1024*1024):
        self.nodes, self.parents = [], {}
        self.disk = None
        self.memory_budget, self.estimated = memory_budget, 0

    def __len__(self):
        return len(self.nodes) + (len(self.disk) if self.disk is not None else 0)

    @property
    def spilled(self):
        return self.disk is not None

    def extend(self, rows):
        source = iter(rows)
        while chunk := list(itertools.islice(source,512)):
            first = len(self)
            if any(row['entry_id'] != f'E{first+i+1}' for i,row in enumerate(chunk)):
                raise ValueError('nonsequential inventory ID')
            cost = sum(256 + 2*(len(row['relative_path'])+len(row['name'])) for row in chunk)
            if self.disk is not None or self.estimated + cost > self.memory_budget:
                if self.disk is None:
                    self.disk = TreeStore()
                self.disk.extend(chunk)
                continue
            for row in chunk:
                self.nodes.append((row['relative_path'],row['name'],row['is_dir'],None))
                parent = row['relative_path'].rpartition('/')[0] or '.'
                self.parents.setdefault(parent, []).append(len(self.nodes))
            self.estimated += cost

    def row(self, identifier):
        relative, name, is_dir, verdict = self.nodes[identifier-1]
        return {'entry_id':f'E{identifier}', 'relative_path':relative, 'name':name,
                'is_dir':is_dir, 'verdict':verdict}

    def children(self, parent='.', after=0, size=80):
        identifiers = self.parents.get(parent, ())
        start = bisect.bisect_right(identifiers, after)
        rows = [self.row(i) for i in identifiers[start:start+size]]
        if self.disk is not None and len(rows) < size:
            rows.extend(self.disk.children(parent, after, size-len(rows)))
        return rows

    def previous(self, parent, after, size=80):
        identifiers = self.parents.get(parent, ())
        stop = bisect.bisect_right(identifiers, after)
        last = list(identifiers[max(0,stop-size-1):stop])
        if self.disk is not None:
            last.extend(row[0] for row in self.disk.db.execute(
                'SELECT id FROM nodes WHERE parent=? AND id<=? ORDER BY id DESC LIMIT ?',
                (self.disk.digest(parent),after,size+1)))
        last.sort()
        return last[-size-1] if len(last) > size else 0

    def mark(self, decisions):
        identifiers = [int(identifier[1:]) for identifier in decisions]
        if any(not 1 <= i <= len(self) for i in identifiers):
            raise ValueError('unknown inventory ID')
        disk_decisions = {f'E{i}':decisions[f'E{i}'] for i in identifiers if i > len(self.nodes)}
        if disk_decisions:
            self.disk.mark(disk_decisions)
        for i in identifiers:
            if i <= len(self.nodes):
                self.nodes[i-1] = (*self.nodes[i-1][:3], decisions[f'E{i}'])

    def close(self):
        self.nodes.clear()
        self.parents.clear()
        if self.disk is not None:
            self.disk.close()
            self.disk = None
