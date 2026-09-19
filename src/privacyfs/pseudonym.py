"""Persistent surface -> alias mapping, stored in SQLite.

The mapping database is sensitive local state; background knowledge can also
enable identification. Default location: %LOCALAPPDATA%\\PrivacyFS.
Aliases are deterministic per database: the same surface always gets the
same alias, so an AI can still reason "these files belong to one person".
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_PREFIX = {
    "PERSON": "PERSON",
    "NAME_LIST": "PERSON",
    "KEYWORD": "SENSITIVE",
    "PATTERN": "PRIVATE",
    "ORG": "ORG",
    "PROFESSION": "ROLE",
    "IDENTITY": "IDENT",
    "LOCATION": "PLACE",
}


def default_db_path() -> Path:
    base = os.environ.get("PRIVACYFS_HOME") or os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".privacyfs"
    root = root / "PrivacyFS"
    root.mkdir(parents=True, exist_ok=True)
    return root / "mapping.db"


class Pseudonymizer:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path.resolve()
        # Autocommit makes every alias allocation visible immediately to other
        # PrivacyFS processes. A busy timeout lets short concurrent writes wait
        # instead of failing with "database is locked".
        self._conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS mapping ("
            "  category TEXT NOT NULL,"
            "  surface TEXT NOT NULL,"
            "  alias TEXT NOT NULL UNIQUE,"
            "  PRIMARY KEY (category, surface)"
            ")"
        )
        # Persistent cache of actual LLM verdicts: filename component -> label.
        # Explicit NONE verdicts are cached; failed, cancelled, and unparsed
        # requests are not, so a later scan can retry them.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache ("
            "  name TEXT PRIMARY KEY,"
            "  label TEXT NOT NULL"
            ")"
        )
        # Additive migration: keep old verdicts untouched, but never assume
        # their unknown model/prompt provenance matches a new detector.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache_v2 ("
            " namespace TEXT NOT NULL, name TEXT NOT NULL, label TEXT NOT NULL,"
            " PRIMARY KEY (namespace, name))"
        )
        # Preload into memory for the common single-process path. alias() still
        # checks SQLite before allocating because another process may have
        # inserted a mapping after this instance started.
        self._cache: dict[tuple[str, str], str] = {
            (c, s): a
            for c, s, a in self._conn.execute("SELECT category, surface, alias FROM mapping")
        }
        self._counters: dict[str, int] = {}

    def _next_number(self, prefix: str) -> int:
        if prefix not in self._counters:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM mapping WHERE alias LIKE ?", (f"{prefix}_%",)
            ).fetchone()
            self._counters[prefix] = row[0]
        self._counters[prefix] += 1
        return self._counters[prefix]

    def alias(self, category: str, surface: str) -> str:
        key = (category, surface)
        requested_key = key
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        row = self._conn.execute(
            "SELECT alias FROM mapping WHERE category = ? AND surface = ?",
            key,
        ).fetchone()
        if row is not None:
            self._cache[key] = row[0]
            return row[0]

        if category in {"PERSON", "NAME_LIST"}:
            alternate = "NAME_LIST" if category == "PERSON" else "PERSON"
            row = self._conn.execute(
                "SELECT alias FROM mapping WHERE category = ? AND surface = ?",
                (alternate, surface),
            ).fetchone()
            if row is not None:
                self._cache[key] = row[0]
                return row[0]
            # New allocations use one PK so concurrent PERSON/NAME_LIST
            # callers cannot create two identities. Existing exact rows win.
            category = "PERSON"
            key = (category, surface)

        prefix = _PREFIX.get(category, "MASKED")
        while True:
            alias = f"{prefix}_{self._next_number(prefix):04d}"
            try:
                self._conn.execute(
                    "INSERT INTO mapping (category, surface, alias) VALUES (?, ?, ?)",
                    (category, surface, alias),
                )
            except sqlite3.IntegrityError:
                # A concurrent process may have inserted either this exact
                # surface or another surface using our locally chosen number.
                row = self._conn.execute(
                    "SELECT alias FROM mapping WHERE category = ? AND surface = ?",
                    key,
                ).fetchone()
                if row is not None:
                    alias = row[0]
                    break
                continue
            break

        self._cache[key] = alias
        self._cache[requested_key] = alias
        return alias

    def all_mappings(self) -> list[tuple[str, str, str]]:
        return self._conn.execute(
            "SELECT category, surface, alias FROM mapping ORDER BY alias"
        ).fetchall()

    def reset(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DELETE FROM mapping")
            self._conn.execute("DELETE FROM llm_cache")
            self._conn.execute("DELETE FROM llm_cache_v2")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._cache.clear()
        self._counters.clear()

    # LLM verdict cache -------------------------------------------------

    def llm_cache_get(self, names: list[str], *, namespace: str = "legacy-api") -> dict[str, str]:
        if not names:
            return {}
        result = {}
        # Below SQLite's historical 999-variable limit, even with namespace.
        for start in range(0, len(names), 500):
            chunk = names[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            result.update(self._conn.execute(
                f"SELECT name, label FROM llm_cache_v2 WHERE namespace = ? AND name IN ({marks})",
                [namespace, *chunk],
            ).fetchall())
        return result

    def llm_cache_put(self, verdicts: dict[str, str], *, namespace: str = "legacy-api") -> None:
        if not verdicts:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO llm_cache_v2 (namespace, name, label) VALUES (?, ?, ?)",
                ((namespace, name, label) for name, label in verdicts.items()),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def llm_cache_for(self, namespace: str):
        return _ScopedVerdicts(self, namespace)

    def close(self) -> None:
        self._conn.close()


class _ScopedVerdicts:
    def __init__(self, owner: Pseudonymizer, namespace: str):
        self.owner, self.namespace = owner, namespace

    def llm_cache_get(self, names):
        return self.owner.llm_cache_get(names, namespace=self.namespace)

    def llm_cache_put(self, verdicts):
        self.owner.llm_cache_put(verdicts, namespace=self.namespace)
