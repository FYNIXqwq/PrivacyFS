"""Internal and public data models for the PrivacyFS scan pipeline.

Models containing ``raw_path`` or ``surface`` are internal-only. Public emitters
must accept ``ScanResult`` / ``SanitizedEntry`` so real filesystem names cannot
cross the output boundary accidentally.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


def make_entry_id(relpath: Path, namespace: str = "scan") -> str:
    """Return a stable opaque identifier without embedding the path itself.

    The identifier is for internal joins and safe reports, not a cryptographic
    anonymization primitive. A release-scoped secret namespace will replace the
    default namespace when scoped releases are introduced in M4.
    """
    digest = hashlib.blake2b(
        relpath.as_posix().encode("utf-8", "surrogatepass"),
        digest_size=12,
        person=b"privacyfs-id",
        key=namespace.encode("utf-8", "surrogatepass")[:64],
    )
    return f"ENTRY_{digest.hexdigest()}"


@dataclass(frozen=True)
class DetectedSignal:
    """One detector finding attached to a path component.

    ``surface`` remains internal and is excluded from repr to reduce accidental
    disclosure through debug logs and exception messages.
    """

    category: str
    surface: str = field(repr=False)
    normalized: str = field(repr=False)
    confidence: float
    source: str
    component_index: int
    relation: str = "DIRECT"
    sensitivity: int = 1

    @classmethod
    def from_finding(
        cls,
        finding,
        *,
        component_index: int,
        source: str,
        confidence: float = 1.0,
    ) -> "DetectedSignal":
        return cls(
            category=finding.category,
            surface=finding.surface,
            normalized=unicodedata.normalize("NFKC", finding.surface).casefold(),
            confidence=confidence,
            source=source,
            component_index=component_index,
        )


@dataclass
class DetectedEntry:
    """Internal path entry after detection but before pseudonymization."""

    entry_id: str
    raw_path: Path = field(repr=False)
    display_path: Path | None = field(default=None, repr=False)
    is_dir: bool = False
    size: int = 0
    depth: int = 0
    hidden: bool = False
    signals: list[DetectedSignal] = field(default_factory=list, repr=False)
    subject_ids: set[str] = field(default_factory=set)
    policy_actions: list[str] = field(default_factory=list)


@dataclass
class SubjectContext:
    """Internal aggregate for entries believed to describe one subject."""

    subject_id: str
    entry_ids: list[str] = field(default_factory=list)
    quasi_identifiers: dict[str, set[str]] = field(default_factory=dict, repr=False)
    sensitive_categories: set[str] = field(default_factory=set)
    category_entries: dict[str, set[str]] = field(default_factory=dict, repr=False)
    top_level_groups: set[str] = field(default_factory=set, repr=False)


@dataclass
class PrivacyGraph:
    """Internal release-level graph of hierarchy, subjects and co-occurrence."""

    subjects: list[SubjectContext]
    entry_subject: dict[str, str]
    parent_edges: tuple[tuple[str, str], ...]
    co_occurrence: dict[str, set[str]]


@dataclass(frozen=True)
class RiskFinding:
    """Safe, serializable explanation of one combination risk."""

    risk_id: str
    risk_type: str
    severity: str
    score: float
    subject_ids: tuple[str, ...]
    entry_ids: tuple[str, ...]
    signal_categories: tuple[str, ...]
    support: int | None
    explanation: str
    recommended_actions: tuple[str, ...]


@dataclass
class AnalysisResult:
    """Public context-analysis report without raw paths or signal values."""

    entries: int
    subjects: int
    risks: list[RiskFinding]
    k_anonymity: int


class ActionType(str, Enum):
    IDENTITY = "identity"
    GENERALIZE = "generalize"
    SUBSTITUTE = "substitute"
    REDACT = "redact"
    DROP = "drop"


@dataclass(frozen=True)
class PolicyAction:
    operator: ActionType
    entry_id: str
    category: str
    replacement: str | None = None


@dataclass
class PolicyResult:
    entries: list[DetectedEntry] = field(repr=False)
    initial_risks: list[RiskFinding] = field(default_factory=list)
    remaining_risks: list[RiskFinding] = field(default_factory=list)
    actions: list[PolicyAction] = field(default_factory=list)
    passes: int = 0
    rejected: bool = False


@dataclass
class MappedEntry:
    """Internal mirror entry retaining the real relative path.

    Only the mirror builder may consume ``raw``. Listings, JSON, logs and TUI
    must use ``SanitizedEntry`` instead.
    """

    raw: Path = field(repr=False)
    clean: str = ""
    is_dir: bool = False
    size: int = 0
    aliases: list[str] = field(default_factory=list)
    hidden: bool = False
    entry_id: str = ""
    collision: bool = False


@dataclass
class SanitizedEntry:
    """Public entry safe for renderers and serialized output."""

    path: str
    is_dir: bool
    size: int
    aliases: list[str] = field(default_factory=list)
    hidden: bool = False
    entry_id: str = ""
    size_label: str | None = None


@dataclass
class ScanResult:
    """Public scan result. It intentionally contains no real path fields."""

    root_label: str
    entries: list[SanitizedEntry]
    files: int | str
    dirs: int | str
    hidden_dirs: int | str
