"""Compact, monotone mappings from a text view to decoded source codepoints."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field


@dataclass(slots=True)
class MappingRun:
    view_start: int
    view_end: int
    source_start: int
    source_end: int
    linear: bool = True


class OffsetMap:
    """Equal-length runs map positions; expansions map to a covering range."""
    def __init__(self):
        self.runs: list[MappingRun] = []
        self._starts: list[int] = []
        self.length = 0

    def append(self, length: int, start: int, end: int, *, linear: bool = True):
        if length <= 0 or start < 0 or end <= start:
            raise ValueError("invalid mapping interval")
        linear = linear and end - start == length
        if self.runs and start < self.runs[-1].source_end:
            raise ValueError("source mapping must be monotone")
        if self.runs and linear and self.runs[-1].linear and self.runs[-1].source_end == start:
            previous = self.runs[-1]
            previous.view_end += length
            previous.source_end = end
        else:
            run = MappingRun(self.length, self.length + length, start, end, linear)
            self.runs.append(run)
            self._starts.append(run.view_start)
        self.length += length

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        if not 0 <= start < end <= self.length:
            raise ValueError("span outside text view")
        first = self.runs[bisect_right(self._starts, start) - 1]
        last = self.runs[bisect_right(self._starts, end - 1) - 1]
        lo = first.source_start + start - first.view_start if first.linear else first.source_start
        hi = last.source_start + end - last.view_start if last.linear else last.source_end
        return lo, hi

    def is_exact(self, start: int, end: int) -> bool:
        self.source_span(start, end)
        first = self.runs[bisect_right(self._starts, start) - 1]
        last = self.runs[bisect_right(self._starts, end - 1) - 1]
        return ((first.linear or start == first.view_start)
                and (last.linear or end == last.view_end))


@dataclass(frozen=True)
class SpanEdit:
    start: int
    end: int
    replacement: str = field(repr=False)


def apply_edits(text: str, edits: list[SpanEdit]) -> str:
    """Apply original leftmost-longest spans once; ties keep input priority.

    This is a scalar editing primitive, not a verified document publisher.
    """
    for edit in edits:
        if not 0 <= edit.start < edit.end <= len(text):
            raise ValueError("edit outside source")
    ordered = sorted(edits, key=lambda e: (e.start, -(e.end - e.start)))
    result, cursor = [], 0
    for edit in ordered:
        if edit.start < cursor:
            continue
        result.extend((text[cursor:edit.start], edit.replacement))
        cursor = edit.end
    result.append(text[cursor:])
    return "".join(result)
