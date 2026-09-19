"""Versioned workspace-local entity resolution; no global identities or storage."""
from __future__ import annotations

from dataclasses import dataclass, field
import secrets

from .relations import EntityType


def opaque(prefix):
    return prefix + "_" + secrets.token_hex(12)


class StaleAnalysisError(ValueError):
    pass


@dataclass(frozen=True)
class Occurrence:
    occurrence_id: str
    entity_type: EntityType
    namespace: str = field(repr=False)
    value: str = field(repr=False)
    locator: object


@dataclass(frozen=True)
class Correction:
    correction_id: str
    workspace_id: str
    before_version: int
    after_version: int
    action: str
    references: tuple[str, ...]


class EntityResolver:
    """Exact (type, namespace, value) keys only. Labels never auto-merge."""
    def __init__(self, workspace_id):
        self.workspace_id = workspace_id
        self.version = 0
        self.occurrences = {}
        self.assignments = {}
        self.status = {}
        self.corrections = []
        self.rejected_assertions = set()
        self.rejected_candidates = set()
        self._keys = {}
        self._sealed = False

    def add(self, entity_type, namespace, value, locator):
        if self._sealed:
            raise ValueError("resolution is sealed")
        key = (entity_type, namespace, value)
        entity = self._keys.setdefault(key, opaque("ENTITY"))
        self.status.setdefault(entity, "explicit")
        occurrence = Occurrence(opaque("OCCURRENCE"), entity_type, namespace, value, locator)
        self.occurrences[occurrence.occurrence_id] = occurrence
        self.assignments[occurrence.occurrence_id] = entity
        return occurrence.occurrence_id

    def seal(self):
        self._sealed = True

    def _check(self, workspace_id, expected_version):
        if workspace_id != self.workspace_id or expected_version != self.version:
            raise StaleAnalysisError("correction scope or version mismatch")
        if not self._sealed:
            raise ValueError("resolution is not ready")

    def _record(self, action, references):
        self.version += 1
        correction = Correction(opaque("CORRECTION"), self.workspace_id, self.version - 1,
                                self.version, action, tuple(references))
        self.corrections.append(correction)
        return correction

    def confirm(self, entity_ids, *, workspace_id, expected_version):
        """Confirm one entity or merge selected typed entities with an audit record."""
        self._check(workspace_id, expected_version)
        selected = set(entity_ids)
        active = set(self.assignments.values())
        if not selected or not selected <= active:
            raise ValueError("unknown entity")
        types = {self.occurrences[o].entity_type for o, e in self.assignments.items() if e in selected}
        namespaces = {self.occurrences[o].namespace for o, e in self.assignments.items() if e in selected}
        if len(types) != 1 or len(namespaces) != 1:
            raise ValueError("incompatible entity types or namespaces")
        target = sorted(selected)[0]
        for occurrence, entity in self.assignments.items():
            if entity in selected:
                self.assignments[occurrence] = target
        self.status[target] = "confirmed"
        return self._record("confirm", sorted(selected))

    def split(self, entity_id, occurrence_ids, *, workspace_id, expected_version):
        self._check(workspace_id, expected_version)
        selected = set(occurrence_ids)
        members = {o for o, e in self.assignments.items() if e == entity_id}
        if not selected or not selected < members:
            raise ValueError("split must select a proper nonempty occurrence subset")
        new_entity = opaque("ENTITY")
        for occurrence in selected:
            self.assignments[occurrence] = new_entity
        self.status[new_entity] = self.status[entity_id] = "confirmed"
        return self._record("split", (entity_id, new_entity, *sorted(selected)))

    def reject_entity(self, entity_id, *, workspace_id, expected_version):
        self._check(workspace_id, expected_version)
        if entity_id not in set(self.assignments.values()):
            raise ValueError("unknown entity")
        self.status[entity_id] = "rejected"
        return self._record("reject_entity", (entity_id,))

    def reject_assertion(self, assertion_id, *, workspace_id, expected_version, known_assertions):
        self._check(workspace_id, expected_version)
        if assertion_id not in known_assertions:
            raise ValueError("unknown assertion")
        self.rejected_assertions.add(assertion_id)
        return self._record("reject_assertion", (assertion_id,))

    def clear(self):
        self.occurrences.clear()
        self.assignments.clear()
        self.status.clear()
        self._keys.clear()
        self.corrections.clear()
        self.rejected_assertions.clear()
        self.rejected_candidates.clear()
