"""Deterministic combination-risk assessment over a PrivacyGraph."""

from __future__ import annotations

import hashlib
from collections import Counter

from .config import Rules
from .context import build_privacy_graph
from .models import AnalysisResult, DetectedEntry, PrivacyGraph, RiskFinding, SubjectContext

_IDENTIFYING = ("DATE", "LOCATION", "ORG", "ROLE", "SCHOOL", "MAJOR", "AWARD")


def _risk_id(risk_type: str, subject_id: str) -> str:
    digest = hashlib.blake2b(
        f"{risk_type}\x1f{subject_id}".encode(),
        digest_size=8,
        person=b"privacyfs-risk",
    ).hexdigest()
    return f"RISK_{digest}"


def _fingerprint(subject: SubjectContext, categories: tuple[str, ...]) -> tuple:
    return tuple(
        (category, tuple(sorted(subject.quasi_identifiers.get(category, ()))))
        for category in categories
    )


def _support_counts(
    subjects: list[SubjectContext],
    categories: tuple[str, ...],
) -> Counter:
    return Counter(
        _fingerprint(subject, categories)
        for subject in subjects
        if all(subject.quasi_identifiers.get(category) for category in categories)
    )


def _relevant_entries(
    subject: SubjectContext,
    categories: tuple[str, ...],
    sensitive: tuple[str, ...] = (),
) -> tuple[str, ...]:
    if not categories and not sensitive:
        return tuple(sorted(subject.entry_ids))
    entry_ids: set[str] = set()
    for category in categories + sensitive:
        entry_ids.update(subject.category_entries.get(category, ()))
    return tuple(sorted(entry_ids))


def _finding(
    subject: SubjectContext,
    *,
    risk_type: str,
    categories: tuple[str, ...],
    sensitive: tuple[str, ...] = (),
    support: int | None,
    score: float,
    explanation: str,
    actions: tuple[str, ...],
) -> RiskFinding:
    all_categories = tuple(sorted(set(categories + sensitive)))
    return RiskFinding(
        risk_id=_risk_id(risk_type, subject.subject_id),
        risk_type=risk_type,
        severity="high" if score >= 8 else "medium",
        score=score,
        subject_ids=(subject.subject_id,),
        entry_ids=_relevant_entries(subject, categories, sensitive),
        signal_categories=all_categories,
        support=support,
        explanation=explanation,
        recommended_actions=actions,
    )


def assess_privacy_graph(graph: PrivacyGraph, k_anonymity: int = 3) -> list[RiskFinding]:
    """Return safe findings for deterministic M2 hard rules and k-support."""
    if k_anonymity < 1:
        raise ValueError("k_anonymity must be positive")
    subjects = graph.subjects
    risks: list[RiskFinding] = []

    rules = (
        (
            "identity_triangulation",
            ("LOCATION", "ORG", "ROLE"),
            (),
            8.0,
            "地点、机构和职业组合在当前发布集合中过于独特。",
            ("generalize_location", "generalize_role", "break_linkage"),
        ),
        (
            "education_reidentification",
            ("SCHOOL", "MAJOR", "DATE"),
            (),
            8.0,
            "学校、专业和时间组合可能重新识别教育经历主体。",
            ("generalize_school", "generalize_date", "break_linkage"),
        ),
        (
            "precise_event_linkage",
            ("DATE_DAY", "LOCATION", "EVENT"),
            (),
            9.0,
            "精确日期、地点和事件组合形成高精度关联。",
            ("generalize_date", "generalize_location", "redact_event"),
        ),
        (
            "legal_linkage",
            ("LOCATION", "DATE"),
            ("LEGAL",),
            10.0,
            "法律事件与地点、时间共同出现，可能定位具体当事人。",
            ("generalize_date", "generalize_location", "suppress_subtree"),
        ),
    )

    support_by_categories = {
        categories: _support_counts(subjects, categories)
        for _, categories, _, _, _, _ in rules
    }
    medical_support_cache: dict[tuple[str, ...], Counter] = {}
    for subject in subjects:
        for risk_type, categories, sensitive, score, explanation, actions in rules:
            if not all(subject.quasi_identifiers.get(category) for category in categories):
                continue
            if not all(category in subject.sensitive_categories for category in sensitive):
                continue
            support = support_by_categories[categories][_fingerprint(subject, categories)]
            if support >= k_anonymity:
                continue
            risks.append(_finding(
                subject,
                risk_type=risk_type,
                categories=categories,
                sensitive=sensitive,
                support=support,
                score=score,
                explanation=explanation,
                actions=actions,
            ))

        if "MEDICAL" in subject.sensitive_categories:
            identifying = tuple(
                category for category in _IDENTIFYING
                if subject.quasi_identifiers.get(category)
            )
            if len(identifying) >= 2:
                counts = medical_support_cache.get(identifying)
                if counts is None:
                    counts = _support_counts(subjects, identifying)
                    medical_support_cache[identifying] = counts
                support = counts[_fingerprint(subject, identifying)]
                if support < k_anonymity:
                    risks.append(_finding(
                        subject,
                        risk_type="medical_reidentification",
                        categories=identifying,
                        sensitive=("MEDICAL",),
                        support=support,
                        score=10.0,
                        explanation="医疗事件与多个准标识符共同出现，可能重新识别主体。",
                        actions=(
                            "generalize_date",
                            "generalize_location",
                            "suppress_subtree",
                        ),
                    ))

        if len(subject.top_level_groups) > 1:
            risks.append(_finding(
                subject,
                risk_type="cross_directory_linkage",
                categories=(),
                support=None,
                score=6.0,
                explanation="明确实体将多个顶层目录关联到同一主体。",
                actions=("break_linkage", "release_scoped_alias"),
            ))

    risks.sort(key=lambda risk: (-risk.score, risk.risk_type, risk.subject_ids))
    return risks


def analyze_entries(
    entries: list[DetectedEntry],
    rules: Rules,
    *,
    namespace: str = "analysis",
    k_anonymity: int | None = None,
) -> AnalysisResult:
    """Build context and return a public, raw-value-free audit result."""
    k = rules.context_k if k_anonymity is None else k_anonymity
    graph = build_privacy_graph(
        entries,
        subject_scope=rules.context_subject_scope,
        namespace=namespace,
    )
    risks = assess_privacy_graph(graph, k)
    return AnalysisResult(
        entries=len(entries),
        subjects=len(graph.subjects),
        risks=risks,
        k_anonymity=k,
    )
