"""Check actual candidate bytes, task fidelity, known residuals and output graph."""
from __future__ import annotations

import csv
import io
from itertools import combinations
from pathlib import Path
import re

from .documents.models import Budget, DocumentSnapshot, Lease, Limits, ParseStatus
from .documents.parsers import parse_bytes
from .evidence import EvidenceGraph, GraphLimits
from .normalization import normalize_with_map
from .relations import FieldSpec, RecordTemplate

VERIFIER_VERSION = "d5-artifact-verifier-v1"


class ReleaseRejected(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class ResidualCheck:
    def __init__(self, terms, budget, *, strict_terms=()):
        self.budget = budget
        self.patterns = []
        strict_terms = set(strict_terms)
        for term in terms:
            budget.check()
            normalized = normalize_with_map(term, check=budget.check).text
            if not normalized:
                continue
            # ASCII IDs are whole tokens; C1 must not match a generated token
            # containing those characters. CJK names match within longer prose.
            expression = re.escape(normalized)
            if term not in strict_terms and normalized[0].isascii() and (normalized[0].isalnum() or normalized[0] == "_"):
                expression = r"(?<![a-z0-9_])" + expression
            if term not in strict_terms and normalized[-1].isascii() and (normalized[-1].isalnum() or normalized[-1] == "_"):
                expression += r"(?![a-z0-9_])"
            self.patterns.append(re.compile(expression))

    def check(self, value):
        self.budget.check()
        normalized = normalize_with_map(value, check=self.budget.check).text
        if any(pattern.search(normalized) for pattern in self.patterns):
            raise ReleaseRejected("KNOWN_VALUE_REMAINS")


def verify_candidate(plan, candidate):
    plan.graph._check(plan.analysis)
    budget = plan.graph._budget()
    residual = ResidualCheck(plan.protected_terms, budget, strict_terms=plan.strict_terms)
    residual.check(".privacyfs-release.json")
    if len(candidate.artifacts) != len(plan.bindings) or sum(len(a.payload) for a in candidate.artifacts) > 64 * 1024 * 1024:
        raise ReleaseRejected("ARTIFACT_LIMIT")
    if len({a.filename.casefold() for a in candidate.artifacts}) != len(candidate.artifacts):
        raise ReleaseRejected("PATH_COLLISION")
    generated, leases = [], []
    try:
        for index, (artifact, records, configured, binding) in enumerate(zip(candidate.artifacts, plan.records, plan.columns, plan.bindings)):
            budget.check()
            expected = tuple(c for c in configured if c.mode != "drop" and (candidate.strategy != "essential_only" or c.required or c.role == "key"))
            expected_format = "csv" if binding[0].snapshot.format in {"docx", "pdf"} else binding[0].snapshot.format
            if artifact.document_index != index or artifact.columns != expected or artifact.format != expected_format:
                raise ReleaseRejected("ARTIFACT_SCHEMA_CHANGED")
            suffix = {"csv": "csv", "markdown": "md", "txt": "txt"}[artifact.format]
            if not re.fullmatch(r"DOC_[0-9a-f]{24}\." + suffix, artifact.filename):
                raise ReleaseRejected("UNSAFE_OUTPUT_PATH")
            residual.check(artifact.filename)
            headers = [c.output_name for c in artifact.columns] + ["assertion_state"]
            for header in headers:
                residual.check(header)
            try:
                text = artifact.payload.decode("utf-8")
                if artifact.format == "csv":
                    rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
                    if not rows or rows.pop(0) != headers:
                        raise ReleaseRejected("OUTPUT_HEADERS_CHANGED")
                else:
                    rows = []
                    for line in text.splitlines():
                        pairs = [part.partition("=") for part in line.split(";")]
                        if [p[0] for p in pairs] != headers or any(p[1] != "=" for p in pairs):
                            raise ReleaseRejected("OUTPUT_RECORD_CHANGED")
                        rows.append([p[2] for p in pairs])
            except (UnicodeError, csv.Error):
                raise ReleaseRejected("OUTPUT_PARSE_FAILED") from None
            # No deletion/reordering of required facts, even if row-count
            # preservation is explicitly disabled for other optional records.
            must_keep = plan.profile.preserve_rows or any(c.required for c in artifact.columns)
            if must_keep and len(rows) != len(records):
                raise ReleaseRejected("TASK_ROWS_CHANGED")
            if rows and len(rows) != len(records):
                raise ReleaseRejected("UNSUPPORTED_ROW_SELECTION")
            for row, source in zip(rows, records):
                budget.check()
                if len(row) != len(headers) or row[-1] != source.state:
                    raise ReleaseRejected("ASSERTION_STATE_CHANGED")
                residual.check(row[-1])
                for value, col in zip(row, artifact.columns):
                    original = source.cells[col.name].value
                    if col.mode == "alias":
                        correct = value == source.aliases.get(col.name, "")
                    elif col.mode == "keep":
                        correct = value == original
                    elif col.mode == "generalize":
                        correct = dict(col.generalizations).get(original) == value
                    else:
                        correct = False
                    if not correct:
                        raise ReleaseRejected("TASK_VALUE_CHANGED")
                    residual.check(value)
                    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
                        raise ReleaseRejected("ACTIVE_SPREADSHEET_VALUE")
            if tuple(map(tuple, rows)) != artifact.rows:
                raise ReleaseRejected("DECLARED_OUTPUT_MISMATCH")
            # Parse the bytes again through D1 and reconstruct the ACTUAL graph.
            lease = Lease()
            leases.append(lease)
            snapshot = DocumentSnapshot(f"OUTPUT_{index}", "OUTPUT_REVISION", "OUTPUT_WORKSPACE", artifact.format,
                len(artifact.payload), Path(artifact.filename), "", _lease=lease)
            limits = Limits(max_seconds=budget.clock.remaining())
            parsed = parse_bytes(snapshot, artifact.payload, limits, Budget(limits), encoding="utf-8")
            if parsed.coverage.status != ParseStatus.COMPLETE:
                raise ReleaseRejected("OUTPUT_COVERAGE_INCOMPLETE")
            source_template = binding[1]
            specs = {f.name: f for f in source_template.fields}
            output_fields = []
            for col in artifact.columns:
                source_spec = specs[col.name]
                role = "label" if col.role == "identity" else col.role
                allowed = tuple(sorted({row[len(output_fields)] for row in rows})) if role == "sensitive" else ()
                output_fields.append(FieldSpec(col.output_name, role, source_spec.namespace,
                    source_spec.entity_type, allowed or source_spec.values if role == "sensitive" else ()))
            primary = next((c.output_name for c in artifact.columns if c.name == source_template.primary), None)
            if primary is None:
                raise ReleaseRejected("OUTPUT_PRIMARY_KEY_MISSING")
            generated.append((parsed, RecordTemplate(tuple(output_fields), primary, "assertion_state")))
        with EvidenceGraph(generated, limits=GraphLimits(max_seconds=budget.clock.remaining())) as output_graph:
            quasi = tuple(sorted({f.name for _, t in generated for f in t.fields if f.role == "quasi"}))
            if len(quasi) > 8:
                raise ReleaseRejected("QUASI_COMBINATION_LIMIT")
            if output_graph.analyze().witnesses:
                raise ReleaseRejected("OUTPUT_RISK_REMAINS")
            # A union of fields can be incomplete for every subject and hide
            # smaller identifying subsets. Check every supported subset.
            for size in range(2, len(quasi) + 1):
                for subset in combinations(quasi, size):
                    budget.check()
                    if output_graph.analyze(quasi_fields=subset).witnesses:
                        raise ReleaseRejected("OUTPUT_RISK_REMAINS")
        budget.check()
        return {"status": "validated", "verifier_version": VERIFIER_VERSION,
                "scope": "configured_task_and_known_values", "privacy_verified": False,
                "artifact_count": len(generated)}
    finally:
        for document, _ in generated:
            document.clear()
        for lease in leases:
            lease.active = False
