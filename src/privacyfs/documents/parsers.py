"""Literal text and strict CSV parsing from captured bytes only."""
from __future__ import annotations

import codecs
import re

from ..normalization import normalize_with_map
from ..source_map import OffsetMap
from .models import (Budget, ContentBlock, DocumentIR, DocumentSnapshot, Limits,
                     ParseCoverage, ParseStatus, ProcessingStopped)

_ENCODINGS = {"utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "gb18030"}
_BOMS = [(codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
         (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
         (codecs.BOM_UTF16_BE, "utf-16-be")]


def failed_document(snapshot, reason, *, unsupported=False):
    return DocumentIR(snapshot, ParseCoverage(
        ParseStatus.UNSUPPORTED if unsupported else ParseStatus.FAILED,
        reason, 0, None), "", _lease=snapshot._lease)


def decode_source(payload: bytes, encoding: str | None):
    selected, bom_size = None, 0
    for bom, candidate in _BOMS:
        if payload.startswith(bom):
            selected, bom_size = candidate, len(bom)
            break
    if encoding is not None:
        if encoding not in _ENCODINGS:
            raise ValueError("UNSUPPORTED_ENCODING")
        if selected is not None and selected != encoding:
            raise ValueError("ENCODING_CONFLICT")
        selected = encoding
    selected = selected or "utf-8"
    try:
        text = payload[bom_size:].decode(selected, errors="strict")
    except UnicodeError:
        raise ValueError("DECODING_FAILED") from None
    if "\x00" in text:
        raise ValueError("BINARY_TEXT")
    return text, selected, bom_size


def _make_block(snapshot, index, value, mapping, limits, budget, *, kind, row=None, column=None, component_index=None):
    budget.check()
    normalized = normalize_with_map(value, check=budget.check, max_chars=limits.max_text_chars)
    return ContentBlock(f"BLOCK_{index:06d}", kind, value, mapping, normalized,
                        snapshot._lease, row, column, component_index)


def _csv_blocks(text, snapshot, limits, budget):
    """Preserve row/cell structure and map decoded quotes back to CSV source.

    Dialect is deliberately explicit: comma, doublequote escaping, no implicit
    whitespace stripping. Blank records and ragged rows are preserved.
    """
    blocks, rows = [], []
    pos, length = 0, len(text)
    while pos < length:
        budget.check()
        row_start = pos
        if len(rows) >= limits.max_csv_rows:
            return blocks, rows, pos, "CSV_ROW_LIMIT"
        # A physical empty record has zero fields, like csv.reader.
        if text[pos] in "\r\n":
            pos += 2 if text.startswith("\r\n", pos) else 1
            rows.append([])
            continue
        row, row_blocks = [], []
        while True:
            if len(row) >= limits.max_csv_columns or len(blocks) + len(row_blocks) >= limits.max_blocks:
                return blocks, rows, row_start, "CSV_STRUCTURE_LIMIT"
            mapping, value = OffsetMap(), []
            quoted = pos < length and text[pos] == '"'
            closed = not quoted
            if quoted:
                pos += 1
            while pos < length:
                if pos % 4096 == 0:
                    budget.check()
                char = text[pos]
                if quoted and char == '"':
                    if pos + 1 < length and text[pos + 1] == '"':
                        if len(value) >= limits.max_cell_chars:
                            return blocks, rows, row_start, "CSV_CELL_LIMIT"
                        value.append('"')
                        mapping.append(1, pos, pos + 2, linear=False)
                        pos += 2
                        continue
                    pos += 1
                    closed = True
                    break
                if not quoted and char in ",\r\n":
                    break
                if len(value) >= limits.max_cell_chars:
                    return blocks, rows, row_start, "CSV_CELL_LIMIT"
                value.append(char)
                mapping.append(1, pos, pos + 1)
                pos += 1
            if not closed or (pos < length and text[pos] not in ",\r\n"):
                raise ValueError("INVALID_CSV")
            decoded = "".join(value)
            block = _make_block(snapshot, len(blocks) + len(row_blocks), decoded, mapping, limits, budget,
                                kind="csv_cell", row=len(rows), column=len(row))
            row.append(decoded)
            row_blocks.append(block)
            if pos == length:
                break
            separator = text[pos]
            pos += 2 if text.startswith("\r\n", pos) else 1
            if separator != ",":
                break
            # A trailing comma produces a final empty cell, even at EOF.
        blocks.extend(row_blocks)
        rows.append(row)
    return blocks, rows, length, None


def parse_bytes(snapshot: DocumentSnapshot, payload: bytes, limits: Limits,
                budget: Budget, *, encoding=None) -> DocumentIR:
    budget.check()
    if snapshot.format in {"docx", "pdf"}:
        from .containers import parse_container
        if encoding is not None:
            raise ValueError("UNSUPPORTED_ENCODING")
        return parse_container(snapshot, payload, limits, budget)
    text, encoding, bom_size = decode_source(payload, encoding)
    if len(text) > limits.max_text_chars:
        raise ValueError("TEXT_LIMIT")
    newline_match = re.search(r"\r\n|\r|\n", text)
    newline = newline_match.group(0) if newline_match else "\n"
    rows, blocks, consumed, reason = [], [], len(text), None
    if snapshot.format == "csv":
        blocks, rows, consumed, reason = _csv_blocks(text, snapshot, limits, budget)
    elif snapshot.format == "path":
        offset = 0
        for index, component in enumerate(text.split("/")):
            if len(blocks) >= limits.max_blocks:
                raise ValueError("BLOCK_LIMIT")
            mapping = OffsetMap()
            if component:
                mapping.append(len(component), offset, offset + len(component))
            blocks.append(_make_block(snapshot, len(blocks), component, mapping, limits, budget,
                                      kind="path_component", component_index=index))
            offset += len(component) + 1
    else:
        mapping = OffsetMap()
        if text:
            mapping.append(len(text), 0, len(text))
        blocks.append(_make_block(snapshot, len(blocks), text, mapping, limits, budget, kind="text"))
    budget.check()
    coverage = ParseCoverage(ParseStatus.PARTIAL if reason else ParseStatus.COMPLETE,
                             reason, consumed, len(text),
                             "filename_only" if snapshot.format == "path" else "literal_file_text")
    return DocumentIR(snapshot, coverage, text, blocks, encoding, bom_size, newline,
                      rows, snapshot._lease, _limits=limits, _cancel=budget.cancel)
