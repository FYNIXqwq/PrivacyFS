"""Bounded local rules baseline. No filename NER/LLM runs on document bodies."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import re

import regex

from ..config import Rules
from ..normalization import NORMALIZATION_VERSION, normalize_with_map
from .models import (Budget, ContentFinding, Limits, ParseStatus, ProcessingStopped)

DETECTOR_VERSION = "d1-rules-v1"
_CATEGORIES = {"NAME_LIST", "PERSON", "ORG", "KEYWORD", "PATTERN", "PROFESSION", "IDENTITY", "LOCATION"}


def _regex_flags(flags):
    result = regex.VERSION0
    for before, after in [(re.I, regex.I), (re.M, regex.M), (re.S, regex.S), (re.X, regex.X), (re.A, regex.A)]:
        if flags & before:
            result |= after
    return result


class RuleInspector:
    def __init__(self, rules: Rules, *, limits=None, cancel=None):
        self.limits, self.cancel = limits or Limits(), cancel
        budget = Budget(self.limits, cancel)
        terms = [(t, "ORG") for t in rules.literal_orgs]
        terms += [(t, "NAME_LIST") for t in rules.literal_names]
        terms += [(t, "KEYWORD") for t in rules.keywords]
        if rules.detect_professions:
            terms += [(t, "PROFESSION") for t in rules.professions]
        if rules.detect_identity:
            terms += [(t, "IDENTITY") for t in rules.identity_terms]
        if len(terms) > 5000 or sum(len(t) for t, _ in terms) > 100_000 or len(rules.regexes) > 100:
            raise ValueError("content rules exceed supported limits")
        self._categories = {}
        for term, category in terms:
            if term:
                normalized = normalize_with_map(term, check=budget.check, max_chars=self.limits.max_text_chars)
                self._categories.setdefault(normalized.text, category)
        pattern = "|".join(regex.escape(t) for t in sorted(self._categories, key=len, reverse=True))
        self._keywords = regex.compile(pattern, regex.VERSION0, cache_pattern=False) if pattern else None
        self._patterns = []
        for pattern in rules.compiled_regexes():
            if len(pattern.pattern) > 16384:
                raise ValueError("content regex exceeds supported size")
            self._patterns.append(regex.compile(pattern.pattern, _regex_flags(pattern.flags), cache_pattern=False))
        budget.check()

    def inspect(self, document):
        _ = document.text
        if document.coverage.status in {ParseStatus.FAILED, ParseStatus.UNSUPPORTED}:
            raise ProcessingStopped(document.coverage.reason or "PARSING_INCOMPLETE")
        inherited = document._limits or self.limits
        limits = replace(self.limits, max_mentions=min(self.limits.max_mentions, inherited.max_mentions),
                         max_seconds=min(self.limits.max_seconds, inherited.max_seconds))
        budget = Budget(limits, (self.cancel, document._cancel))
        found, seen = [], set()

        def emit(block, start, end, category, *, normalized):
            if start == end:
                return
            covering = False
            if normalized:
                covering = not block.normalized.mapping.is_exact(start, end)
                start, end = block.normalized.source_span(start, end)
            location = document.locate(block, start, end, covering=covering)
            key = (block.block_id, location.start, location.end, category)
            if key in seen:
                return
            if len(found) >= limits.max_mentions:
                raise ProcessingStopped("FINDING_LIMIT")
            seen.add(key)
            found.append(ContentFinding(category, location, block.text[start:end]))

        try:
            for block in document.blocks:
                budget.check()
                if self._keywords:
                    for match in self._keywords.finditer(block.normalized.text, timeout=budget.remaining()):
                        emit(block, match.start(), match.end(), self._categories[match.group(0)], normalized=True)
                        budget.check()
                for pattern in self._patterns:
                    for normalized, text in ((False, block.text), (True, block.normalized.text)):
                        for match in pattern.finditer(text, timeout=budget.remaining()):
                            emit(block, match.start(), match.end(), "PATTERN", normalized=normalized)
                            budget.check()
        except TimeoutError:
            raise ProcessingStopped("TIME_LIMIT") from None
        budget.check()
        return sorted(found, key=lambda f: (f.locator.start, f.locator.end, f.category))


def inspect_document(document, rules: Rules | None = None, *, limits=None, cancel=None):
    limits = limits or document._limits
    return RuleInspector(rules or Rules(), limits=limits,
                         cancel=(cancel, document._cancel)).inspect(document)


def public_report(document, findings, *, inspection_reason=None):
    """Explicit allowlist boundary: no raw path, value, hash or exception text."""
    _ = document.text
    complete = document.coverage.status is ParseStatus.COMPLETE and inspection_reason is None
    categories = Counter(f.category if f.category in _CATEGORIES else "OTHER" for f in findings)
    return {
        "document_id": document.snapshot.document_id,
        "revision_id": document.snapshot.revision_id,
        "format": document.snapshot.format,
        "status": "failed" if inspection_reason else document.coverage.status.value,
        "reason": inspection_reason or document.coverage.reason,
        "scope": document.coverage.scope,
        "parsing_status": document.coverage.status.value,
        "unprocessed": list(document.coverage.unprocessed),
        "projection_complete": document.coverage.projection_complete,
        "total_parts": document.coverage.total_parts,
        "parsed_characters": document.coverage.parsed_characters,
        "total_characters": document.coverage.total_characters,
        "finding_count": len(findings) if complete else None,
        "category_counts": dict(sorted(categories.items())),
        "detector_version": "d1-path-adapter-v1" if document.snapshot.format == "path" else DETECTOR_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "privacy_verified": False,
        "findings": [{
            "category": f.category if f.category in _CATEGORIES else "OTHER",
            "block_id": f.locator.block_id,
            "start": f.locator.start, "end": f.locator.end,
            "line": f.locator.line, "row": f.locator.row, "column": f.locator.column,
            "unit": f.locator.unit, "covering": f.locator.covering,
            "page": f.locator.page, "paragraph": f.locator.paragraph, "table": f.locator.table,
        } for f in findings],
    }
