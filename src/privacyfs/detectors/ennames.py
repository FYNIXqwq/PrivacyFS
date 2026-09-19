"""English personal-name detection: "First Last" pairs from built-in
common-name dictionaries (top US names). Dictionary membership is required
on BOTH parts so phrases like "New Folder" or "Hello World" don't match."""

from __future__ import annotations

import re

from .keywords import Finding

_GIVEN = frozenset(
    "james john robert michael william david richard joseph thomas charles "
    "christopher daniel matthew anthony mark donald steven paul andrew joshua "
    "kenneth kevin brian george timothy ronald edward jason jeffrey ryan "
    "jacob gary nicholas eric jonathan stephen larry justin scott brandon "
    "benjamin samuel gregory alexander patrick frank raymond jack dennis "
    "jerry tyler aaron jose adam nathan henry zachary douglas peter kyle "
    "mary patricia jennifer linda elizabeth barbara susan jessica sarah "
    "karen nancy lisa betty margaret sandra ashley kimberly emily donna "
    "michelle dorothy carol amanda melissa deborah stephanie rebecca sharon "
    "laura cynthia kathleen amy shirley angela helen anna brenda pamela "
    "nicole emma samantha katherine christine debra rachel catherine carolyn "
    "janet ruth maria olivia heather virginia judy cheryl megan andrea".split()
)

_SURNAMES = frozenset(
    "smith johnson williams brown jones garcia miller davis rodriguez "
    "martinez hernandez lopez gonzalez wilson anderson thomas taylor moore "
    "jackson martin lee perez thompson white harris sanchez clark ramirez "
    "lewis robinson walker young allen king wright scott torres nguyen hill "
    "flores green adams nelson baker hall rivera campbell mitchell carter "
    "roberts gomez phillips evans turner diaz parker cruz edwards collins "
    "reyes stewart morris morales murphy cook rogers gutierrez ortiz morgan "
    "cooper peterson bailey reed kelly howard ramos kim cox ward richardson".split()
)

_PAIR = re.compile(r"\b([A-Z][a-z]{1,15})[ .·]([A-Z][a-z]{1,15})\b")


class EnNameDetector:
    """Matches "John Smith" style pairs (space, dot, or middle-dot)."""

    def find(self, name: str) -> list[Finding]:
        out: list[Finding] = []
        for m in _PAIR.finditer(name):
            first, last = m.group(1).lower(), m.group(2).lower()
            if (first in _GIVEN and last in _SURNAMES) or (
                first in _SURNAMES and last in _GIVEN
            ):
                out.append(Finding("PERSON", m.group(0)))
        return out
