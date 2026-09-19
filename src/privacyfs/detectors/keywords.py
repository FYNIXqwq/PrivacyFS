"""Keyword / literal-name detection over path names."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    category: str   # KEYWORD | NAME_LIST | PATTERN | PERSON
    surface: str    # the real matched text -- never emit this in output


class KeywordDetector:
    """One combined alternation regex over (term, category) pairs.

    A single compiled pattern keeps matching O(text) with one regex pass per
    name instead of one pass per keyword.
    """

    def __init__(self, terms: list[tuple[str, str]]):
        self._categories: dict[str, str] = {}
        self._matched_categories: dict[str, str] = {}
        pats = []
        plain_pats = []
        seen = set()
        # Capture the matching alternative: Unicode regex case equivalence
        # cannot safely be reconstructed by lower()/casefold() dictionary keys.
        for term, category in sorted(terms, key=lambda item: -len(item[0])):
            if not term or term in seen:
                continue
            seen.add(term)
            group = f"term_{len(pats)}"
            self._categories[group] = category
            pattern = re.escape(term)
            # Context hints are words, not arbitrary fragments of software
            # names (COO in cookie, offer in coffers, visa in visage).
            # Explicit names and keywords retain their substring contract.
            if category in {"PROFESSION", "IDENTITY"} and re.fullmatch(
                    r"[A-Za-z]+(?: [A-Za-z]+)*", unicodedata.normalize("NFKC", term)):
                letters = "A-Za-zＡ-Ｚａ-ｚ"
                pattern = rf"(?<![{letters}]){pattern}(?![{letters}])"
            pats.append(f"(?P<{group}>{pattern})")
            plain_pats.append(pattern)
        self._pattern = (
            re.compile("|".join(plain_pats), re.IGNORECASE)
            if pats
            else None
        )
        self._classifier = re.compile("|".join(pats), re.IGNORECASE) if pats else None

    def find(self, name: str) -> list[Finding]:
        if self._pattern is None:
            return []
        findings = []
        for match in self._pattern.finditer(name):
            surface = match.group(0)
            category = self._matched_categories.get(surface)
            if category is None:
                matched = self._classifier.fullmatch(surface)
                category = self._categories[matched.lastgroup]
                if len(self._matched_categories) < 4096:
                    self._matched_categories[surface] = category
            findings.append(Finding(category, surface))
        return findings


class RegexDetector:
    def __init__(self, patterns: list[re.Pattern]):
        self._patterns = patterns

    def find(self, name: str) -> list[Finding]:
        out: list[Finding] = []
        for pat in self._patterns:
            for m in pat.finditer(name):
                out.append(Finding("PATTERN", m.group(0)))
        return out
