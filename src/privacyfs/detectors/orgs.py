"""Organization-name detection: CJK-prefix + org-suffix patterns."""

from __future__ import annotations

import re

from .keywords import Finding

# English orgs: "Acme Inc", "Foo Bar LLC", "X GmbH"
_EN_ORG = re.compile(
    r"\b[A-Z][\w&]*(?:[ -][A-Z][\w&]*){0,3}[ -]?"
    r"(?:Inc|LLC|Ltd|Corp|Co|GmbH|SARL|BV)\b\.?"
)


class OrgDetector:
    """Matches runs like "某某公司" / "某某大学" / "Acme Inc".

    The CJK branch needs at least 2 chars before the suffix, so generic
    phrases starting with the suffix itself ("公司注册流程") don't match.
    It deliberately over-captures leading context ("我的某司" masks
    "我的某司") -- over-masking is the safe direction for this tool.
    """

    def __init__(self, suffixes: list[str]):
        alt = "|".join(sorted((re.escape(s) for s in suffixes), key=len, reverse=True))
        self._cn = re.compile(rf"[一-鿿]{{2,15}}?(?:{alt})") if alt else None

    def find(self, name: str) -> list[Finding]:
        out: list[Finding] = []
        if self._cn is not None:
            out.extend(Finding("ORG", m.group(0)) for m in self._cn.finditer(name))
        out.extend(Finding("ORG", m.group(0)) for m in _EN_ORG.finditer(name))
        return out
