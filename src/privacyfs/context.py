"""Release-level context construction for inference-risk analysis."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from .detectors.context_terms import ContextTermDetector
from .detectors.dates import DateContextDetector
from .detectors.keywords import Finding
from .detectors.locations import LocationContextDetector
from .models import (
    DetectedEntry,
    DetectedSignal,
    PrivacyGraph,
    SubjectContext,
)

_CONTEXT_DETECTORS = (
    DateContextDetector(),
    LocationContextDetector(),
    ContextTermDetector(),
)
_SENSITIVE_CATEGORIES = {
    "MEDICAL", "LEGAL", "FINANCIAL", "POLITICAL", "FAMILY",
    "IDENTITY", "KEYWORD", "PATTERN",
}
# Generic ORG detections do not merge subjects: coworkers at one institution
# are not necessarily one person. Explicit configured names arrive as
# NAME_LIST and may safely provide a stronger cross-directory link.
_ENTITY_CATEGORIES = {"PERSON", "NAME_LIST"}
_SCHOOL_SUFFIXES = (
    "大学", "学院", "中学", "小学", "学校", "幼儿园",
    "大學", "學院", "中學", "小學", "學校", "幼兒園",
)


def _subject_id(groups: set[str], namespace: str) -> str:
    material = "\x1f".join(sorted(groups)).encode("utf-8", "surrogatepass")
    digest = hashlib.blake2b(
        material,
        digest_size=8,
        person=b"privacyfs-subj",
        key=namespace.encode("utf-8", "surrogatepass")[:64],
    ).hexdigest()
    return f"SUBJECT_{digest}"


def _entry_path(entry: DetectedEntry) -> Path:
    return entry.display_path if entry.display_path is not None else entry.raw_path


def enrich_context_signals(entries: list[DetectedEntry]) -> list[DetectedEntry]:
    """Attach context-only signals without changing ordinary scan masking.

    The operation is idempotent so callers may safely analyze a previously
    enriched collection.
    """
    component_cache: dict[str, list[tuple[Finding, str]]] = {}
    for entry in entries:
        existing = {
            (signal.category, signal.surface, signal.component_index, signal.source)
            for signal in entry.signals
        }
        for component_index, component in enumerate(_entry_path(entry).parts):
            cached = component_cache.get(component)
            if cached is None:
                cached = []
                for detector in _CONTEXT_DETECTORS:
                    source = type(detector).__name__
                    cached.extend((finding, source) for finding in detector.find(component))
                component_cache[component] = cached
            for finding, source in cached:
                key = (finding.category, finding.surface, component_index, source)
                if key in existing:
                    continue
                entry.signals.append(DetectedSignal.from_finding(
                    finding,
                    component_index=component_index,
                    source=source,
                ))
                existing.add(key)
    return entries


class _UnionFind:
    def __init__(self, values: set[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _top_level_group(entry: DetectedEntry) -> str:
    # Root-level files share one subject instead of each filename becoming a
    # unique subject. A top-level directory and all descendants share its name.
    path = _entry_path(entry)
    if len(path.parts) == 1 and not entry.is_dir:
        # A flat directory commonly contains one file per person. Keep each
        # root file independent unless an explicit entity signal links it to
        # another file; otherwise unrelated weak signals are combined into a
        # synthetic person.
        return f"__ROOT_FILE__:{entry.entry_id}"
    return path.parts[0] if path.parts else "__EMPTY_ROOT__"


def _add_quasi(
    subject: SubjectContext,
    category: str,
    value: str,
    entry_id: str,
) -> None:
    subject.quasi_identifiers.setdefault(category, set()).add(value)
    subject.category_entries.setdefault(category, set()).add(entry_id)


def _accumulate_signal(
    subject: SubjectContext,
    signal: DetectedSignal,
    entry_id: str,
) -> None:
    category = signal.category
    value = signal.normalized

    if category.startswith("DATE_"):
        _add_quasi(subject, "DATE", value, entry_id)
        _add_quasi(subject, category, value, entry_id)
    elif category.startswith("LOCATION_"):
        _add_quasi(subject, "LOCATION", value, entry_id)
        _add_quasi(subject, category, value, entry_id)
    elif category == "ORG":
        _add_quasi(subject, "ORG", value, entry_id)
        if signal.surface.endswith(_SCHOOL_SUFFIXES):
            _add_quasi(subject, "SCHOOL", value, entry_id)
    elif category == "PROFESSION":
        _add_quasi(subject, "ROLE", value, entry_id)
    elif category in {"EDUCATION", "MAJOR", "AWARD"}:
        _add_quasi(subject, category, value, entry_id)
    elif category in {"MEDICAL", "LEGAL", "FINANCIAL", "POLITICAL", "FAMILY"}:
        _add_quasi(subject, "EVENT", value, entry_id)
        subject.category_entries.setdefault(category, set()).add(entry_id)

    if category in _SENSITIVE_CATEGORIES:
        subject.sensitive_categories.add(category)
        subject.category_entries.setdefault(category, set()).add(entry_id)


def build_privacy_graph(
    entries: list[DetectedEntry],
    *,
    subject_scope: str = "top_level_directory",
    namespace: str = "analysis",
) -> PrivacyGraph:
    """Build hierarchy, co-occurrence and subject clusters for one release."""
    enrich_context_signals(entries)
    if subject_scope not in {"top_level_directory", "whole_release"}:
        raise ValueError(f"unsupported subject scope: {subject_scope}")

    group_for_entry: dict[str, str] = {}
    groups: set[str] = set()
    for entry in entries:
        group = "__WHOLE_RELEASE__" if subject_scope == "whole_release" else _top_level_group(entry)
        group_for_entry[entry.entry_id] = group
        groups.add(group)

    union = _UnionFind(groups)
    if subject_scope == "top_level_directory":
        owner_by_entity: dict[tuple[str, str], str] = {}
        for entry in entries:
            group = group_for_entry[entry.entry_id]
            org_values = {signal.normalized for signal in entry.signals if signal.category == "ORG"}
            for signal in entry.signals:
                if signal.category not in _ENTITY_CATEGORIES:
                    continue
                if signal.category == "NAME_LIST" and signal.normalized in org_values:
                    continue
                # PERSON and explicit NAME_LIST are alternate detector labels
                # for the same entity kind and must link across categories.
                entity_kind = (
                    "PERSON"
                    if signal.category in {"PERSON", "NAME_LIST"}
                    else signal.category
                )
                key = (entity_kind, signal.normalized)
                previous = owner_by_entity.setdefault(key, group)
                union.union(previous, group)

    merged_groups: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        merged_groups[union.find(group)].add(group)

    subject_by_root: dict[str, SubjectContext] = {}
    for root, members in merged_groups.items():
        subject_by_root[root] = SubjectContext(
            subject_id=_subject_id(members, namespace),
            top_level_groups=set(members),
        )

    entry_subject: dict[str, str] = {}
    for entry in entries:
        root = union.find(group_for_entry[entry.entry_id])
        subject = subject_by_root[root]
        subject.entry_ids.append(entry.entry_id)
        entry.subject_ids = {subject.subject_id}
        entry_subject[entry.entry_id] = subject.subject_id
        _add_quasi(subject, "STRUCTURE_DEPTH", str(entry.depth), entry.entry_id)
        for signal in entry.signals:
            _accumulate_signal(subject, signal, entry.entry_id)

    id_by_path = {_entry_path(entry).as_posix(): entry.entry_id for entry in entries}
    parent_edges: list[tuple[str, str]] = []
    for entry in entries:
        parent = _entry_path(entry).parent
        if parent == Path("."):
            continue
        parent_id = id_by_path.get(parent.as_posix())
        if parent_id is not None:
            parent_edges.append((parent_id, entry.entry_id))

    subjects = sorted(subject_by_root.values(), key=lambda subject: subject.subject_id)
    for subject in subjects:
        subject.entry_ids.sort()
    co_occurrence = {
        subject.subject_id: set(subject.quasi_identifiers) | set(subject.sensitive_categories)
        for subject in subjects
    }
    return PrivacyGraph(
        subjects=subjects,
        entry_subject=entry_subject,
        parent_edges=tuple(sorted(parent_edges)),
        co_occurrence=co_occurrence,
    )
