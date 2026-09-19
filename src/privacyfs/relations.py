"""D2 explicit record templates. Configuration and extracted values stay private."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path

from .documents.models import ParseStatus, ProcessingStopped


class EntityType(str, Enum):
    PERSON = "person"
    ORGANIZATION = "organization"
    CLIENT = "client"
    PROJECT = "project"
    EVENT = "event"


class AssertionState(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    QUOTED = "quoted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FieldSpec:
    name: str = field(repr=False)
    role: str
    namespace: str = field(default="", repr=False)
    entity_type: EntityType = EntityType.CLIENT
    values: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or len(self.name) > 128:
            raise ValueError("invalid field name")
        if self.role not in {"key", "identity", "sensitive", "quasi", "label"}:
            raise ValueError("invalid field role")
        object.__setattr__(self, "entity_type", EntityType(self.entity_type))
        if not isinstance(self.namespace, str) or len(self.namespace) > 128 or (self.role == "key" and not self.namespace):
            raise ValueError("key requires a namespace")
        if not isinstance(self.values, tuple) or len(self.values) > 1000 or any(not isinstance(v, str) or not v or len(v) > 1024 for v in self.values):
            raise ValueError("invalid allowed values")
        if self.role == "sensitive" and not self.values:
            raise ValueError("sensitive fields require explicit allowed values")


@dataclass(frozen=True)
class RecordTemplate:
    fields: tuple[FieldSpec, ...] = field(repr=False)
    primary: str = field(repr=False)
    state_field: str | None = field(default=None, repr=False)
    default_state: AssertionState = AssertionState.POSITIVE
    source_view: str | None = None
    table_index: int | None = None
    pages: tuple[int, ...] = ()

    def __post_init__(self):
        if not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 32 or any(not isinstance(f, FieldSpec) for f in self.fields):
            raise ValueError("invalid template fields")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names) or self.primary not in names:
            raise ValueError("ambiguous template fields")
        if next(f for f in self.fields if f.name == self.primary).role != "key":
            raise ValueError("primary field must be a key")
        if self.state_field is not None and (not isinstance(self.state_field, str) or not self.state_field or self.state_field in names or len(self.state_field) > 128):
            raise ValueError("invalid state field")
        object.__setattr__(self, "default_state", AssertionState(self.default_state))
        if self.source_view not in {None, "docx_paragraphs", "docx_table", "pdf_pages"}:
            raise ValueError("unsupported source projection")
        if self.source_view == "docx_table":
            if type(self.table_index) is not int or self.table_index < 0:
                raise ValueError("explicit table index required")
        elif self.table_index is not None:
            raise ValueError("table index is only valid for docx_table")
        if not isinstance(self.pages, tuple) or len(self.pages) > 100 or any(type(p) is not int or p < 1 for p in self.pages) or tuple(sorted(set(self.pages))) != self.pages:
            raise ValueError("invalid selected pages")
        if self.source_view == "pdf_pages" and not self.pages or self.source_view != "pdf_pages" and self.pages:
            raise ValueError("explicit PDF page selection required")


@dataclass(frozen=True)
class Cell:
    value: str = field(repr=False)
    locator: object


def extract_records(document, template, budget):
    """Yield row/line dictionaries. No inferred headers, prose, or cross-row facts."""
    complex_input = document.snapshot.format in {"docx", "pdf"}
    if complex_input:
        if document.coverage.status not in {ParseStatus.COMPLETE, ParseStatus.PARTIAL} or not document.coverage.projection_complete:
            raise ProcessingStopped("PROJECTION_INCOMPLETE")
        allowed = {"docx": {"docx_paragraphs", "docx_table"}, "pdf": {"pdf_pages"}}
        if template.source_view not in allowed[document.snapshot.format]:
            raise ProcessingStopped("EXPLICIT_PROJECTION_REQUIRED")
    elif document.coverage.status is not ParseStatus.COMPLETE:
        raise ProcessingStopped("PARSING_INCOMPLETE")
    elif template.source_view is not None:
        raise ValueError("source projection does not match input format")
    _ = document.text
    expected = {f.name for f in template.fields}
    if template.state_field:
        expected.add(template.state_field)
    if document.snapshot.format == "csv" or template.source_view == "docx_table":
        if template.source_view == "docx_table":
            selected = [b for b in document.blocks if b.kind == "docx_cell" and b.table == template.table_index]
            if not selected:
                raise ProcessingStopped("SELECTED_TABLE_MISSING")
            blocks = {(b.row, b.column): b for b in selected}
            grouped = {}
            for block in selected:
                grouped.setdefault(block.row, {})[block.column] = block.text
            rows = []
            for row_index in range(max(grouped) + 1):
                budget.check()
                row = grouped.get(row_index, {})
                if sorted(row) != list(range(len(row))):
                    raise ProcessingStopped("TEMPLATE_MISMATCH")
                rows.append([row[c] for c in range(len(row))])
        else:
            rows = document.csv_rows
            blocks = {(b.row, b.column): b for b in document.blocks}
        if not rows or len(set(rows[0])) != len(rows[0]) or not expected.issubset(rows[0]):
            raise ProcessingStopped("TEMPLATE_MISMATCH")
        columns = {name: rows[0].index(name) for name in expected}
        for row_index, row in enumerate(rows[1:], 1):
            budget.check()
            if len(row) != len(rows[0]):
                raise ProcessingStopped("TEMPLATE_MISMATCH")
            cells = {}
            for name, col in columns.items():
                block = blocks[row_index, col]
                cells[name] = Cell(block.text, document.locate(block, 0, len(block.text)) if block.text else None)
            yield cells, False
    elif document.snapshot.format in {"txt", "markdown"} or complex_input:
        # Exact one-record-per-line grammar: name=value;name=value.
        if template.source_view == "docx_paragraphs":
            selected = [b for b in document.blocks if b.kind == "docx_paragraph" and b.text.strip()]
        elif template.source_view == "pdf_pages":
            if any(p > (document.coverage.total_parts or 0) for p in template.pages):
                raise ProcessingStopped("SELECTED_PAGE_MISSING")
            selected = [b for b in document.blocks if b.page in template.pages]
            if len(selected) != len(template.pages) or any(not b.text.strip() for b in selected):
                raise ProcessingStopped("SELECTED_PAGE_HAS_NO_TEXT")
        else:
            selected = document.blocks[:1]
        if complex_input and not selected:
            raise ProcessingStopped("SELECTED_TEXT_MISSING")
        for block in selected:
            yield from _line_records(document, block, expected, budget)
    else:
        raise ProcessingStopped("UNSUPPORTED_FORMAT")


def _line_records(document, block, expected, budget):
    offset = 0
    for line in block.text.splitlines(keepends=True):
        budget.check()
        raw = line.rstrip("\r\n")
        quoted = raw.startswith(">")
        prefix = 1 if quoted else 0
        cursor, cells = offset + prefix, {}
        if raw[prefix:].strip():
            for part in raw[prefix:].split(";"):
                name, sep, value = part.partition("=")
                if not sep or name not in expected or name in cells:
                    raise ProcessingStopped("TEMPLATE_MISMATCH")
                start = cursor + len(name) + 1
                cells[name] = Cell(value, document.locate(block, start, start + len(value)) if value else None)
                cursor += len(part) + 1
            if cells.keys() != expected:
                raise ProcessingStopped("TEMPLATE_MISMATCH")
            yield cells, quoted
        offset += len(line)


def load_relation_config(path: Path):
    """Bounded strict JSON; paths resolved later through DocumentSession."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate configuration key")
            result[key] = value
        return result
    with path.open("rb") as stream:
        payload = stream.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("configuration limit")
    try:
        config = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=unique)
        if set(config) != {"schema_version", "files"} or config["schema_version"] != "d2-templates-1":
            raise ValueError("invalid relation configuration")
        if not isinstance(config["files"], list) or not 1 <= len(config["files"]) <= 1000:
            raise ValueError("invalid file bindings")
        bindings, paths = [], set()
        for item in config["files"]:
            if set(item) - {"path", "fields", "primary", "state_field", "default_state", "encoding", "source_view", "table_index", "pages"}:
                raise ValueError("unknown binding field")
            if not isinstance(item["path"], str) or item["path"] in paths:
                raise ValueError("invalid file binding")
            paths.add(item["path"])
            specs = []
            for value in item["fields"]:
                if set(value) - {"name", "role", "namespace", "entity_type", "values"}:
                    raise ValueError("unknown field setting")
                value = dict(value)
                if "values" in value:
                    if not isinstance(value["values"], list):
                        raise ValueError("invalid allowed values")
                    value["values"] = tuple(value["values"])
                specs.append(FieldSpec(**value))
            if not isinstance(item.get("pages", []), list):
                raise ValueError("invalid selected pages")
            template = RecordTemplate(tuple(specs), item["primary"], item.get("state_field"), item.get("default_state", "positive"),
                                      item.get("source_view"), item.get("table_index"), tuple(item.get("pages", [])))
            if item.get("encoding") is not None and not isinstance(item["encoding"], str):
                raise ValueError("invalid encoding")
            bindings.append((item["path"], template, item.get("encoding")))
        return bindings
    except (KeyError, TypeError, AttributeError, RecursionError):
        raise ValueError("invalid relation configuration") from None
