"""Date precision signals used only by context analysis."""

from __future__ import annotations

import re

from .keywords import Finding

_DAY = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:[-_.年])(?:0?[1-9]|1[0-2])"
    r"(?:[-_.月])(?:0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_MONTH = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?:[-_.年])(?:0?[1-9]|1[0-2])月?(?![-_.月\d])"
)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}年?(?!\d)")


class DateContextDetector:
    """Classify dates by day, month, or year precision."""

    def find(self, text: str) -> list[Finding]:
        occupied: list[tuple[int, int]] = []
        findings: list[Finding] = []
        for category, pattern in (
            ("DATE_DAY", _DAY),
            ("DATE_MONTH", _MONTH),
            ("DATE_YEAR", _YEAR),
        ):
            for match in pattern.finditer(text):
                span = match.span()
                if any(span[0] >= start and span[1] <= end for start, end in occupied):
                    continue
                occupied.append(span)
                findings.append(Finding(category, match.group(0)))
        return findings
