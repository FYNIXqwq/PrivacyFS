"""Deterministic attackers independent of PrivacyFS detectors and private gold.

Views contain only serialized public table values and declared prior knowledge.
This is an API information boundary, not a sandbox for hostile Python plugins.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Table:
    kind: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def records(self):
        return tuple(dict(zip(self.columns, row)) for row in self.rows)


@dataclass(frozen=True)
class Knowledge:
    identity: str = ""
    key: str = ""
    topic: str = ""


@dataclass(frozen=True)
class AttackView:
    tables: tuple[Table, ...]
    background: tuple[Knowledge, ...] = ()
    history: tuple[Knowledge, ...] = ()


@dataclass(frozen=True)
class Claims:
    identities: frozenset[tuple[str, str]] = frozenset()
    attributes: frozenset[tuple[str, str]] = frozenset()


def records(view, kind):
    return [row for table in view.tables if table.kind == kind for row in table.records()]


def subject_facts(view):
    """Exact directed keys, not row order or untyped arbitrary string matches."""
    bridges = records(view, "bridges")
    return [(b["cid"], f) for b in bridges for f in records(view, "facts")
            if b.get("cid") and b.get("mid") and b["mid"] == f.get("mid")]


@dataclass(frozen=True)
class Attacker:
    name: str
    version: str = "explicit-attacks-v1"

    def run(self, view):
        identities = {(r["cid"], r["name"]) for r in records(view, "clients")
                      if r.get("cid") and r.get("name")}
        if self.name == "direct_identity":
            return Claims(frozenset(identities))
        linked = subject_facts(view)
        if self.name in {"background_linkage", "history_linkage"}:
            known = list(view.background)
            if self.name == "history_linkage":
                known.extend(view.history)
            # Public historic keys can connect a named profile with a topic.
            expanded = known + [Knowledge(a.identity, a.key, b.topic)
                                for a in known for b in known
                                if a.identity and a.key and a.key == b.key and b.topic]
            subjects = {r["cid"] for r in records(view, "clients") if r.get("cid")}
            for subject in subjects:
                topics = {f.get("topic") for cid, f in linked if cid == subject and f.get("topic")}
                guesses = {k.identity for k in expanded if k.identity and (
                    (k.key and k.key == subject) or (k.topic and k.topic in topics))}
                # Do not cherry-pick the first candidate from an ambiguous set.
                if len(guesses) == 1:
                    identities.add((subject, next(iter(guesses))))
        attributes = {(identity, fact["fact"]) for subject, identity in identities
                      for cid, fact in linked if cid == subject and fact.get("fact")
                      and fact.get("assertion_state") == "positive"}
        return Claims(frozenset(identities), frozenset(attributes))


ATTACKS = {name: Attacker(name) for name in (
    "direct_identity", "key_join", "background_linkage", "history_linkage")}
