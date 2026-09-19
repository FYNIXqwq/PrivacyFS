"""D1 normalization with source covers, without context/entity inference.

NFKC + casefold + grapheme-local OpenCC t2s. Script conversion is intentionally
local, not phrase translation. A normalization match is not an entity identity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import unicodedata
from importlib.metadata import version

import regex

from .source_map import OffsetMap

NORMALIZATION_VERSION = (
    f"d1-nfkc-casefold-grapheme-t2s-v1/ucd-{unicodedata.unidata_version}"
    f"/regex-{regex.__version__}/opencc-{version('opencc-python-reimplemented')}"
)
_SIMPLE = r"\x00-\x7f\u3000-\u3029\u3400-\u9fff\uff01-\uff5e"
# Leave the last simple character to \X when followed by a possible combining
# mark/variation selector. Inside these runs characters cannot compose across
# boundaries, so equal-length conversions share a single linear mapping run.
_GRAPHEME = regex.compile(rf"(?P<simple>[{_SIMPLE}]+(?=[{_SIMPLE}]|$))|\X", regex.VERSION0)


@dataclass
class NormalizedText:
    text: str = field(repr=False)
    mapping: OffsetMap = field(repr=False)
    version: str = NORMALIZATION_VERSION

    def source_span(self, start: int, end: int):
        return self.mapping.source_span(start, end)

    def clear(self):
        self.text = ""
        self.mapping = OffsetMap()


def normalize_with_map(text: str, *, check=None, max_chars=4_000_000,
                       max_cluster_chars=4096) -> NormalizedText:
    from .config import to_simplified

    mapping = OffsetMap()
    if check:
        check()
    if len(text) > max_chars:
        raise ValueError("NORMALIZATION_LIMIT")
    if not text:
        return NormalizedText("", mapping)
    if text.isascii():
        mapping.append(len(text), 0, len(text))
        return NormalizedText(text.casefold(), mapping)
    output, length = [], 0
    # This per-call cache holds individual public character transformations,
    # never whole document fragments or cross-workspace personal-name strings.
    single_char_cache = {}
    translation = {}
    nonlinear_simple = set()
    def convert(fragment):
        cached = single_char_cache.get(fragment) if len(fragment) == 1 else None
        if cached is not None:
            return cached
        value = unicodedata.normalize("NFKC", fragment).casefold()
        if any("\u3400" <= char <= "\u9fff" for char in value):
            value = to_simplified(value)
        if len(fragment) == 1 and len(single_char_cache) < 4096:
            single_char_cache[fragment] = value
        return value
    for index, match in enumerate(_GRAPHEME.finditer(text)):
        if check and index % 256 == 0:
            check()
        cluster = match.group(0)
        simple = match.lastgroup == "simple"
        if not simple and len(cluster) > max_cluster_chars:
            raise ValueError("GRAPHEME_LIMIT")
        if simple:
            for char in set(cluster):
                key = ord(char)
                if key not in translation:
                    translation[key] = convert(char)
                    if len(translation[key]) != 1:
                        nonlinear_simple.add(char)
        converted = cluster.translate(translation) if simple else convert(cluster)
        if not converted:
            raise ValueError("EMPTY_NORMALIZATION")
        length += len(converted)
        if length > max_chars:
            raise ValueError("NORMALIZATION_LIMIT")
        if simple and nonlinear_simple.intersection(cluster):
            for offset, char in enumerate(cluster):
                value = translation[ord(char)]
                mapping.append(len(value), match.start() + offset, match.start() + offset + 1)
        else:
            mapping.append(len(converted), match.start(), match.end(), linear=simple or len(cluster) == 1)
        if len(mapping.runs) > 100_000:
            raise ValueError("MAPPING_LIMIT")
        output.append(converted)
    if check:
        check()
    return NormalizedText("".join(output), mapping)
