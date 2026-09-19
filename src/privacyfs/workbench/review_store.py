"""Human decisions stay separate from the original model candidates."""
from datetime import datetime, timezone
import json

from ..gui.results import ResultStore

STATES = ("pending", "confirmed", "false_positive", "defer")


class ReviewStore(ResultStore):
    def __init__(self):
        super().__init__()
        self.db.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, state TEXT NOT NULL, detail BLOB NOT NULL)")
        self.db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, previous BLOB NOT NULL)")
        self.revision = 0
        self._counts_cache = None

    def extend(self, rows):
        first = self.count + 1
        super().extend({**row, "_review_id":first + i} for i, row in enumerate(rows))

    def decision(self, identifier):
        record = self.db.execute("SELECT detail FROM decisions WHERE id=?", (identifier,)).fetchone()
        return self.decode(record[0]) if record else {"state":"pending", "note":"", "updated_at":None}

    def state(self, identifier):
        row = self.db.execute("SELECT state FROM decisions WHERE id=?", (identifier,)).fetchone()
        return row[0] if row else "pending"

    def _set(self, identifier, value):
        if type(identifier) is not int or not 1 <= identifier <= self.count:
            raise ValueError("invalid review target")
        if (value.get("state") not in STATES or not isinstance(value.get("note"), str)
                or len(value["note"]) > 2000):
            raise ValueError("invalid review decision")
        payload = self.provider.protect(json.dumps(value, ensure_ascii=True).encode("utf-8"))
        self.db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?)", (identifier, value["state"], payload))

    def decide(self, identifiers, state, note=""):
        identifiers = list(dict.fromkeys(identifiers))
        if (not identifiers or len(identifiers) > 80 or state not in STATES
                or note is not None and (not isinstance(note, str) or len(note) > 2000)):
            raise ValueError("invalid review operation")
        previous = [(identifier, self.decision(identifier)) for identifier in identifiers]
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.db:
            for identifier, old in previous:
                self._set(identifier, {"state":state, "note":old["note"] if note is None else note,
                                       "updated_at":timestamp})
            self.db.execute("INSERT INTO history(previous) VALUES (?)", (
                self.provider.protect(json.dumps(previous, ensure_ascii=True).encode("utf-8")),))
        self.revision += 1

    def undo(self):
        last = self.db.execute("SELECT id,previous FROM history ORDER BY id DESC LIMIT 1").fetchone()
        if last is None:
            return False
        with self.db:
            for identifier, value in self.decode(last[1]):
                self._set(identifier, value)
            self.db.execute("DELETE FROM history WHERE id=?", (last[0],))
        self.revision += 1
        return True

    @property
    def can_undo(self):
        return self.db.execute("SELECT 1 FROM history LIMIT 1").fetchone() is not None

    def counts(self):
        key = (self.count, self.revision)
        if self._counts_cache is not None and self._counts_cache[0] == key:
            return dict(self._counts_cache[1])
        result = dict.fromkeys(STATES, 0)
        result.update(dict(self.db.execute("SELECT state,COUNT(*) FROM decisions GROUP BY state")))
        result["pending"] = self.count - sum(result[state] for state in STATES if state != "pending")
        self._counts_cache = (key, result)
        return dict(result)

    def decorate(self, row):
        return {**row, "human_review":self.decision(row["_review_id"])}

    def iter_reviewed(self, scope="all"):
        if scope not in {"all", "confirmed"}:
            raise ValueError("invalid export scope")
        for row in self:
            value = self.decorate(row)
            if scope == "all" or value["human_review"]["state"] == "confirmed":
                yield value

    def review_page(self, number, size, query="", kind=0, categories=(), state="all"):
        if state not in (*STATES, "all"):
            raise ValueError("invalid review filter")
        predicate = None if state == "all" else lambda row: self.state(row["_review_id"]) == state
        rows, count, pending, number = super().page(number, size, query, kind, categories,
            predicate=predicate, filter_token=(self.revision if state != "all" else 0, state))
        return [self.decorate(row) for row in rows], count, pending, number

    def import_rows(self, rows):
        first = self.count + 1
        plain = [{k:v for k,v in row.items() if k not in {"human_review", "_review_id"}} for row in rows]
        self.extend(plain)
        with self.db:
            for offset, row in enumerate(rows):
                self._set(first + offset, row["human_review"])
