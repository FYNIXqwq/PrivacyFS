"""Deterministic M3 declassification and context-enforcement policies."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import Path

from .config import Rules
from .models import (
    ActionType,
    DetectedEntry,
    MappedEntry,
    PolicyAction,
    PolicyResult,
    ScanResult,
)
from .risk import analyze_entries

_CONTEXT_SOURCES = {
    "DateContextDetector",
    "LocationContextDetector",
    "ContextTermDetector",
}
_SCHOOL_SUFFIXES = (
    "大学", "学院", "中学", "小学", "学校", "幼儿园",
    "大學", "學院", "中學", "小學", "學校", "幼兒園",
)
_SENSITIVE_EVENTS = {"MEDICAL", "LEGAL", "FINANCIAL", "POLITICAL", "FAMILY"}


class PolicyRejected(RuntimeError):
    """Raised when enforce mode cannot eliminate all high risks."""

    def __init__(self, result: PolicyResult):
        self.result = result
        super().__init__(
            f"context policy left {len(result.remaining_risks)} risk(s) after "
            f"{result.passes} pass(es)"
        )


def apply_operator(
    value: str,
    operator: ActionType,
    replacement: str | None = None,
) -> str | None:
    """Apply one generic declassification operator to a scalar value."""
    if operator is ActionType.IDENTITY:
        return value
    if operator is ActionType.DROP:
        return None
    if operator is ActionType.REDACT:
        return replacement or "[REDACTED]"
    if operator in {ActionType.GENERALIZE, ActionType.SUBSTITUTE}:
        if replacement is None:
            raise ValueError(f"{operator.value} requires a replacement")
        return replacement
    raise ValueError(f"unsupported operator: {operator}")


def generalize_date(value: str) -> str:
    """Reduce date precision by one level: day -> month -> year -> marker."""
    match = re.search(r"(?<!\d)((?:19|20)\d{2})[-_.年](\d{1,2})[-_.月](\d{1,2})日?(?!\d)", value)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    match = re.search(r"(?<!\d)((?:19|20)\d{2})[-_.年](\d{1,2})月?(?!\d)", value)
    if match:
        return match.group(1)
    if re.search(r"(?<!\d)(?:19|20)\d{2}年?(?!\d)", value):
        return "[DATE]"
    return "[DATE]"


def generalize_location(value: str) -> str:
    """Keep an enclosing city when explicitly present, otherwise use a marker."""
    city = re.search(r"([一-鿿]{2,8}市)", value)
    if city and not value.endswith("市"):
        return city.group(1)
    return "[LOCATION]"


def generalize_role(value: str) -> str:
    lower = value.casefold()
    if any(term in lower for term in ("工程师", "程序员", "设计师", "cto", "技术")):
        return "技术人员"
    if any(term in lower for term in ("经理", "总监", "总裁", "董事长", "ceo", "cfo", "coo")):
        return "管理人员"
    if any(term in lower for term in ("医生", "护士", "医师")):
        return "医疗人员"
    if any(term in lower for term in ("教师", "老师", "教授")):
        return "教育人员"
    return "[ROLE]"


def generalize_school(_value: str) -> str:
    return "[EDUCATION_ORG]"


def bucket_count(value: int) -> str:
    if value <= 0:
        return "0"
    for upper in (4, 9, 19, 49, 99, 499, 999):
        lower = 1 if upper == 4 else ({9: 5, 19: 10, 49: 20, 99: 50, 499: 100, 999: 500}[upper])
        if value <= upper:
            return f"{lower}-{upper}"
    return "1000+"


def bucket_size(value: int) -> str:
    if value <= 0:
        return "0 B"
    kib = 1024
    mib = 1024 * kib
    gib = 1024 * mib
    if value < kib:
        return "<1 KB"
    if value < 10 * kib:
        return "1-10 KB"
    if value < 100 * kib:
        return "10-100 KB"
    if value < mib:
        return "100 KB-1 MB"
    if value < 10 * mib:
        return "1-10 MB"
    if value < 100 * mib:
        return "10-100 MB"
    if value < gib:
        return "100 MB-1 GB"
    return "1 GB+"


def _entry_path(entry: DetectedEntry) -> Path:
    return entry.display_path if entry.display_path is not None else entry.raw_path


def _replace_surface(entry: DetectedEntry, surface: str, replacement: str) -> bool:
    path = _entry_path(entry).as_posix()
    if surface not in path:
        return False
    entry.display_path = Path(path.replace(surface, replacement))
    entry.depth = len(entry.display_path.parts)
    return True


def strip_context_signals(entries: list[DetectedEntry]) -> None:
    for entry in entries:
        entry.signals = [
            signal for signal in entry.signals if signal.source not in _CONTEXT_SOURCES
        ]
        entry.subject_ids.clear()


def _generalize_risk(
    entries: list[DetectedEntry],
    risk,
    actions: list[PolicyAction],
) -> bool:
    changed = False
    targets = set(risk.entry_ids)
    requested = set(risk.recommended_actions)
    for entry in entries:
        if entry.entry_id not in targets:
            continue
        retained = []
        for signal in entry.signals:
            replacement = None
            category = signal.category
            strongest = risk.risk_type in {
                "medical_reidentification", "legal_linkage"
            }
            if "generalize_date" in requested and category.startswith("DATE_"):
                replacement = "[DATE]" if strongest else generalize_date(signal.surface)
            elif "generalize_location" in requested and category.startswith("LOCATION_"):
                replacement = "[LOCATION]" if strongest else generalize_location(signal.surface)
            elif "generalize_role" in requested and category == "PROFESSION":
                replacement = generalize_role(signal.surface)
            elif (
                "generalize_school" in requested
                and category == "ORG"
                and signal.surface.endswith(_SCHOOL_SUFFIXES)
            ):
                replacement = generalize_school(signal.surface)
            elif "redact_event" in requested and category in _SENSITIVE_EVENTS:
                replacement = "[EVENT]"

            if replacement is None or not _replace_surface(entry, signal.surface, replacement):
                retained.append(signal)
                continue
            changed = True
            entry.policy_actions.append(f"generalize:{category}")
            actions.append(PolicyAction(
                ActionType.GENERALIZE,
                entry.entry_id,
                category,
                replacement,
            ))
        entry.signals = retained
    return changed


def _starts_with(path: Path, prefix: Path) -> bool:
    return path.parts[:len(prefix.parts)] == prefix.parts


def _suppress_sensitive_subtrees(
    entries: list[DetectedEntry],
    risk,
    actions: list[PolicyAction],
) -> tuple[list[DetectedEntry], bool]:
    if "suppress_subtree" not in risk.recommended_actions:
        return entries, False
    sensitive = "MEDICAL" if risk.risk_type == "medical_reidentification" else "LEGAL"
    label = f"[{sensitive}_SUBTREE]"
    targets = set(risk.entry_ids)
    prefixes: dict[tuple[str, ...], tuple[Path, int]] = {}
    for entry in entries:
        if entry.entry_id not in targets:
            continue
        path = _entry_path(entry)
        for signal in entry.signals:
            if signal.category != sensitive or signal.component_index >= len(path.parts):
                continue
            raw_prefix = Path(*path.parts[:signal.component_index + 1])
            placeholder = Path(*path.parts[:signal.component_index], label)
            prefixes[raw_prefix.parts] = (placeholder, signal.component_index)

    if not prefixes:
        return entries, False
    ordered = sorted(
        (
            (Path(*parts), placeholder, cutoff)
            for parts, (placeholder, cutoff) in prefixes.items()
        ),
        key=lambda item: len(item[0].parts),
    )
    kept: list[DetectedEntry] = []
    emitted: set[str] = set()
    for entry in entries:
        path = _entry_path(entry)
        match = next((item for item in ordered if _starts_with(path, item[0])), None)
        if match is None:
            kept.append(entry)
            continue
        _prefix, placeholder, cutoff = match
        placeholder_key = placeholder.as_posix()
        if placeholder_key in emitted:
            actions.append(PolicyAction(ActionType.DROP, entry.entry_id, sensitive))
            continue
        emitted.add(placeholder_key)
        replacement = copy.deepcopy(entry)
        replacement.display_path = placeholder
        replacement.is_dir = True
        replacement.hidden = True
        replacement.depth = len(placeholder.parts)
        # Preserve signals from safe parent components so the later ordinary
        # pseudonymizer/generalizers still protect names and organizations in
        # the placeholder's parent path. Drop the sensitive component itself.
        replacement.signals = [
            signal for signal in replacement.signals
            if signal.component_index < cutoff
        ]
        replacement.subject_ids.clear()
        replacement.policy_actions.append(f"suppress_subtree:{sensitive}")
        kept.append(replacement)
        actions.append(PolicyAction(
            ActionType.REDACT,
            entry.entry_id,
            sensitive,
            label,
        ))
    return kept, True


def _apply_risks(
    entries: list[DetectedEntry],
    risks,
) -> tuple[list[DetectedEntry], list[PolicyAction], bool]:
    actions: list[PolicyAction] = []
    changed = False
    working = entries
    # Suppression first: later generalizers must not spend work on entries that
    # are about to disappear.
    for risk in risks:
        if risk.severity != "high":
            continue
        working, suppressed = _suppress_sensitive_subtrees(working, risk, actions)
        changed = changed or suppressed
    for risk in risks:
        if risk.severity == "high":
            changed = _generalize_risk(working, risk, actions) or changed
    return working, actions, changed


def enforce_context_policy(
    entries: list[DetectedEntry],
    rules: Rules,
    *,
    namespace: str,
) -> PolicyResult:
    """Iteratively transform entries until no high combination risks remain."""
    working = copy.deepcopy(entries)
    strip_context_signals(working)
    initial = analyze_entries(working, rules, namespace=namespace).risks
    remaining = initial
    all_actions: list[PolicyAction] = []
    passes = 0

    while any(risk.severity == "high" for risk in remaining):
        if passes >= rules.context_max_policy_passes:
            break
        working, actions, changed = _apply_risks(working, remaining)
        all_actions.extend(actions)
        passes += 1
        if not changed:
            break
        strip_context_signals(working)
        remaining = analyze_entries(working, rules, namespace=namespace).risks

    rejected = any(risk.severity == "high" for risk in remaining)
    # analyze_entries attaches context-only signals for the final assessment;
    # remove them before handing entries back to the ordinary pseudonymizer.
    strip_context_signals(working)
    result = PolicyResult(
        entries=working,
        initial_risks=initial,
        remaining_risks=remaining,
        actions=all_actions,
        passes=passes,
        rejected=rejected,
    )
    if rejected and rules.context_fail_closed:
        raise PolicyRejected(result)
    return result


def blur_scan_result(result: ScanResult) -> ScanResult:
    """Hide exact counts and sizes in an enforced public result."""
    result.files = bucket_count(int(result.files))
    result.dirs = bucket_count(int(result.dirs))
    result.hidden_dirs = bucket_count(int(result.hidden_dirs))
    for entry in result.entries:
        entry.size_label = bucket_size(entry.size)
    return result


def blur_mapped_structure(mapped: list[MappedEntry]) -> list[MappedEntry]:
    """Collapse long single-directory chains into a neutral [PATH] node.

    The top-level subject label and final leaf remain available for utility;
    intermediate one-child directory names and exact depth are removed.
    """
    dirs = {entry.clean for entry in mapped if entry.is_dir and not entry.hidden}
    children: dict[str, set[str]] = defaultdict(set)
    for entry in mapped:
        parent = entry.clean.rsplit("/", 1)[0] if "/" in entry.clean else ""
        children[parent].add(entry.clean)

    replacements: list[tuple[str, str, set[str]]] = []
    occupied = {entry.clean for entry in mapped}
    for root in sorted(path for path in dirs if "/" not in path):
        chain: list[str] = []
        current = root
        while True:
            kids = children.get(current, set())
            if len(kids) != 1:
                break
            child = next(iter(kids))
            if child not in dirs:
                break
            chain.append(child)
            current = child
        if len(chain) >= 2:
            neutral = f"{root}/[PATH]"
            suffix = 2
            while neutral in occupied and neutral not in set(chain):
                neutral = f"{root}/[PATH]__{suffix}"
                suffix += 1
            replacements.append((chain[-1], neutral, set(chain[:-1])))
            occupied.add(neutral)

    if not replacements:
        return mapped
    output: list[MappedEntry] = []
    for entry in mapped:
        if any(entry.clean in removed for _, _, removed in replacements):
            continue
        clone = copy.copy(entry)
        for old_prefix, new_prefix, _removed in replacements:
            if clone.clean == old_prefix or clone.clean.startswith(old_prefix + "/"):
                clone.clean = new_prefix + clone.clean[len(old_prefix):]
                break
        output.append(clone)
    output.sort(key=lambda entry: entry.clean)
    return output
