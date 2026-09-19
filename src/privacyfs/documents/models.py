"""Private source models. Only the explicit public_report adapter may export."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
import re
import time

from ..normalization import NormalizedText
from ..source_map import OffsetMap


class ParseStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ClosedSessionError(RuntimeError):
    pass


class StaleLocatorError(ValueError):
    pass


class ProcessingStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_files: int = 1000
    max_text_chars: int = 4_000_000
    max_total_chars: int = 16_000_000
    max_blocks: int = 100_000
    max_csv_rows: int = 100_000
    max_csv_columns: int = 1024
    max_cell_chars: int = 65_536
    max_mentions: int = 10_000
    max_seconds: float = 10.0
    max_container_bytes: int = 32 * 1024 * 1024
    max_zip_entries: int = 1000
    max_pdf_pages: int = 100
    max_worker_memory_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        for name, value in vars(self).items():
            if name == "max_seconds":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid processing timeout")
            elif isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("invalid processing limit")


class Budget:
    def __init__(self, limits: Limits, cancel=None):
        self.deadline = time.monotonic() + limits.max_seconds
        pending, events = [cancel], []
        while pending:
            event = pending.pop()
            if isinstance(event, tuple):
                pending.extend(event)
            elif event is not None:
                events.append(event)
        self.cancel = tuple(events)

    def check(self):
        if any(event.is_set() for event in self.cancel):
            raise ProcessingStopped("CANCELLED")
        if time.monotonic() >= self.deadline:
            raise ProcessingStopped("TIME_LIMIT")

    def remaining(self):
        self.check()
        return max(0.000001, self.deadline - time.monotonic())


@dataclass
class Lease:
    active: bool = True

    def check(self):
        if not self.active:
            raise ClosedSessionError("document session is closed")


@dataclass(frozen=True)
class DocumentSnapshot:
    document_id: str
    revision_id: str
    workspace_id: str
    format: str
    size_bytes: int
    source_path: Path = field(repr=False)
    content_digest: str = field(repr=False)
    capture_reason: str | None = None
    _lease: Lease = field(default_factory=Lease, repr=False, compare=False)


@dataclass(frozen=True)
class ParseCoverage:
    status: ParseStatus
    reason: str | None
    parsed_characters: int
    total_characters: int | None
    scope: str = "literal_file_text"
    parser_version: str = "d1-text-csv-v1"
    unprocessed: tuple[str, ...] = ()
    projection_complete: bool = False
    total_parts: int | None = None


@dataclass(frozen=True)
class SourceLocator:
    document_id: str
    revision_id: str
    block_id: str
    start: int
    end: int
    block_start: int
    block_end: int
    line: int
    row: int | None = None
    column: int | None = None
    component_index: int | None = None
    unit: str = "decoded_unicode_codepoint"
    covering: bool = False
    part: str | None = None
    page: int | None = None
    paragraph: int | None = None
    table: int | None = None


@dataclass
class ContentBlock:
    block_id: str
    kind: str
    _text: str = field(repr=False)
    source_map: OffsetMap = field(repr=False)
    normalized: NormalizedText = field(repr=False)
    _lease: Lease = field(repr=False)
    row: int | None = None
    column: int | None = None
    component_index: int | None = None
    _disposed: bool = field(default=False, repr=False)
    part: str | None = None
    page: int | None = None
    paragraph: int | None = None
    table: int | None = None

    @property
    def text(self):
        self._lease.check()
        if self._disposed:
            raise ClosedSessionError("document handle has been released")
        return self._text

    def source_span(self, start, end):
        _ = self.text
        return self.source_map.source_span(start, end)

    def clear(self):
        self._disposed = True
        self._text = ""
        self.normalized.clear()
        self.source_map = OffsetMap()


@dataclass
class DocumentIR:
    snapshot: DocumentSnapshot
    coverage: ParseCoverage
    _text: str = field(repr=False)
    blocks: list[ContentBlock] = field(default_factory=list, repr=False)
    encoding: str | None = None
    bom_bytes: int = 0
    newline: str = "\n"
    _rows: list[list[str]] = field(default_factory=list, repr=False)
    _lease: Lease = field(default_factory=Lease, repr=False)
    _line_starts: list[int] = field(default_factory=list, repr=False)
    _block_index: dict[str, ContentBlock] = field(default_factory=dict, repr=False)
    _disposed: bool = field(default=False, repr=False)
    _limits: Limits | None = field(default=None, repr=False)
    _cancel: object = field(default=None, repr=False)

    def __post_init__(self):
        self._line_starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", self._text)]
        self._block_index = {block.block_id: block for block in self.blocks}

    @property
    def text(self):
        self._lease.check()
        if self._disposed:
            raise ClosedSessionError("document handle has been released")
        return self._text

    @property
    def csv_rows(self):
        _ = self.text
        return [list(row) for row in self._rows]

    def locate(self, block: ContentBlock, start: int, end: int, *, covering=False):
        _ = self.text
        if self._block_index.get(block.block_id) is not block:
            raise ValueError("block does not belong to document")
        lo, hi = block.source_span(start, end)
        return SourceLocator(self.snapshot.document_id, self.snapshot.revision_id,
            block.block_id, lo, hi, start, end, bisect_right(self._line_starts, lo),
            block.row, block.column, block.component_index,
            unit="extracted_unicode_codepoint" if self.snapshot.format in {"docx", "pdf"} else "decoded_unicode_codepoint",
            covering=covering, part=block.part, page=block.page, paragraph=block.paragraph, table=block.table)

    def resolve(self, locator: SourceLocator) -> str:
        _ = self.text
        if (locator.document_id, locator.revision_id) != (self.snapshot.document_id, self.snapshot.revision_id):
            raise StaleLocatorError("locator belongs to a different document revision")
        block = self._block_index.get(locator.block_id)
        if block is None or block.source_span(locator.block_start, locator.block_end) != (locator.start, locator.end):
            raise ValueError("inconsistent source locator")
        return self._text[locator.start:locator.end]

    def clear(self):
        self._disposed = True
        self._text = ""
        self._rows.clear()
        self._line_starts.clear()
        self._block_index.clear()
        for block in self.blocks:
            block.clear()
        self.blocks.clear()


@dataclass(frozen=True)
class ContentFinding:
    category: str
    locator: SourceLocator
    surface: str = field(repr=False)
    detector: str = "d1-rules-v1"
