"""Conservative location signals used by release-level context analysis."""

from __future__ import annotations

import re

from .keywords import Finding

_MUNICIPALITIES = re.compile(r"北京|上海|天津|重庆|重慶|香港|澳门|澳門")
_ADMIN = re.compile(
    r"[一-鿿]{2,8}(?:特别行政区|特別行政區|自治区|自治區|自治州|省|市|区|區|县|縣)"
)


class LocationContextDetector:
    """Detect explicit Chinese administrative-place names and precision."""

    def find(self, text: str) -> list[Finding]:
        findings: list[Finding] = []
        occupied: list[tuple[int, int]] = []
        for match in _ADMIN.finditer(text):
            surface = match.group(0)
            if surface.endswith(("区", "區", "县", "縣", "自治州")):
                category = "LOCATION_DISTRICT"
            elif surface.endswith("市"):
                category = "LOCATION_CITY"
            else:
                category = "LOCATION_PROVINCE"
            findings.append(Finding(category, surface))
            occupied.append(match.span())
        for match in _MUNICIPALITIES.finditer(text):
            if any(match.start() >= start and match.end() <= end for start, end in occupied):
                continue
            findings.append(Finding("LOCATION_CITY", match.group(0)))
        return findings
