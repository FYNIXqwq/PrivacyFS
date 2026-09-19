"""Finite D3 candidate plans over explicit records, never over unparsed bytes."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import csv
import hashlib
import hmac
import io
import json
import secrets

from .relations import AssertionState, extract_records
from .task_profiles import TaskProfile

PLANNER_VERSION = "d5-projection-v1"


@dataclass(frozen=True)
class Action:
    document_index: int
    row: int
    field_index: int | None
    kind: str
    evidence_ids: tuple[str, ...]
    locators: tuple = field(repr=False)


@dataclass(frozen=True)
class Column:
    name: str = field(repr=False)
    output_name: str
    mode: str
    role: str
    required: bool
    generalizations: tuple = field(default=(), repr=False)


@dataclass(frozen=True)
class Record:
    cells: dict = field(repr=False)
    aliases: dict = field(repr=False)
    state: str


@dataclass(frozen=True)
class Artifact:
    document_index: int
    filename: str
    format: str
    columns: tuple[Column, ...] = field(repr=False)
    rows: tuple[tuple[str, ...], ...] = field(repr=False)
    payload: bytes = field(repr=False)


@dataclass(frozen=True)
class Candidate:
    strategy: str
    artifacts: tuple[Artifact, ...] = field(repr=False)
    actions: tuple[Action, ...] = field(repr=False)
    loss: int
    feasible: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ReleasePlan:
    scope_id: str
    graph: object = field(repr=False)
    analysis: object = field(repr=False)
    profile: TaskProfile = field(repr=False)
    bindings: tuple = field(repr=False)
    records: tuple = field(repr=False)
    columns: tuple = field(repr=False)
    protected_terms: tuple[str, ...] = field(repr=False)
    strict_terms: tuple[str, ...] = field(repr=False)
    candidates: tuple[Candidate, ...] = field(repr=False)
    selected: int | None


def serialize_rows(format, columns, rows):
    headers = [c.output_name for c in columns] + ["assertion_state"]
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
        return stream.getvalue().encode("utf-8")
    for row in rows:
        if any(any(ch in value for ch in ";\r\n") for value in row):
            raise ValueError("text record cannot serialize this value")
    return "".join(";".join(f"{name}={value}" for name, value in zip(headers, row)) + "\n" for row in rows).encode("utf-8")


def build_plan(bindings, graph, analysis, profile, *, seed=None):
    from .verifier import ReleaseRejected, verify_candidate
    graph._check(analysis)
    if not isinstance(profile, TaskProfile):
        raise ValueError("invalid task profile")
    bindings = tuple(bindings)
    if {d.snapshot.document_id for d, _ in bindings} != set(graph._documents):
        raise ValueError("plan documents must match the graph")
    if len(bindings) != len(graph._documents) or any(graph._documents[d.snapshot.document_id] is not d
            or graph._templates[d.snapshot.document_id] != t for d, t in bindings):
        raise ValueError("plan snapshot or template differs from the analyzed graph")
    seed = secrets.token_bytes(32) if seed is None else seed
    if not isinstance(seed, bytes) or len(seed) != 32:
        raise ValueError("invalid release seed")
    budget = graph._budget()
    indices = {d.snapshot.document_id: i for i, (d, _) in enumerate(bindings)}
    members = {}
    for oid, occurrence in graph.resolver.occurrences.items():
        budget.check()
        entity = graph.resolver.assignments[oid]
        loc = occurrence.locator
        members.setdefault(entity, []).append((indices[loc.document_id], loc.start, loc.end,
            occurrence.entity_type.value, occurrence.namespace, occurrence.value))
    aliases = {}
    for entity, group in members.items():
        budget.check()
        token = hmac.new(seed, json.dumps(sorted(group), ensure_ascii=False).encode("utf-8"), hashlib.sha256).hexdigest()[:24]
        aliases[entity] = group[0][3].upper() + "_" + token
    if len(set(aliases.values())) != len(aliases):
        raise ReleaseRejected("ALIAS_COLLISION")
    locations = {(o.locator.document_id, o.locator.start, o.locator.end): aliases[graph.resolver.assignments[oid]]
                 for oid, o in graph.resolver.occurrences.items()}
    evidence_at = {}
    for e in analysis.evidence:
        for aid in e.assertion_ids:
            for loc in graph._raw[aid].locators:
                evidence_at.setdefault((loc.document_id, loc.start, loc.end), set()).add(e.evidence_id)
    policies = {f.name: f for f in profile.fields}
    all_names = {f.name for _, t in bindings for f in t.fields}
    if not set(policies) <= all_names:
        raise ValueError("task refers to an unknown field")
    all_columns, all_records, protected = [], [], set(profile.forbidden_terms)
    strict_terms = set(profile.forbidden_terms)
    key_names = sorted({f.name for _, t in bindings for f in t.fields if f.role == "key"})
    for document, template in bindings:
        budget.check()
        columns = []
        for spec in template.fields:
            policy = policies.get(spec.name)
            mode = policy.mode if policy else ("alias" if spec.role == "key" else "drop")
            if spec.role in {"key", "identity"} and mode not in {"alias", "drop"}:
                raise ValueError("identity and key fields cannot retain their raw values")
            if spec.role == "key" and mode == "drop" and (spec.name == template.primary or profile.preserve_relations):
                raise ValueError("required key relationship cannot be dropped")
            if spec.role not in {"key", "identity"} and mode == "alias":
                raise ValueError("alias is defined only for typed entities")
            if mode == "keep" and not (policy.approved_values or spec.values):
                raise ValueError("retained factual fields require an explicit value allowlist")
            output_name = policy.output_name if policy else (f"key_{key_names.index(spec.name) + 1}" if spec.role == "key" else f"removed_{len(columns) + 1}")
            columns.append(Column(spec.name, output_name, mode, spec.role,
                policy.required if policy else spec.role == "key" and profile.preserve_relations,
                policy.generalizations if policy else ()))
        active_names = [c.output_name for c in columns if c.mode != "drop"]
        if len(set(active_names)) != len(active_names):
            raise ValueError("ambiguous output headers")
        records = []
        for cells, quoted in extract_records(document, template, budget):
            budget.check()
            state = "quoted" if quoted else cells[template.state_field].value if template.state_field else template.default_state.value
            if state not in {"positive", "negative", "quoted"}:
                raise ReleaseRejected("UNRESOLVED_ASSERTION")
            row_aliases = {}
            for spec in template.fields:
                cell = cells[spec.name]
                if spec.role in {"key", "identity"} and cell.value:
                    protected.add(cell.value)
                    if spec.role == "identity":
                        strict_terms.add(cell.value)
                    locator = cell.locator if spec.role == "key" else cells[template.primary].locator
                    row_aliases[spec.name] = locations[locator.document_id, locator.start, locator.end]
            for col in columns:
                if col.mode == "keep":
                    spec = next(f for f in template.fields if f.name == col.name)
                    allowed = policies[col.name].approved_values or spec.values
                    if cells[col.name].value not in allowed:
                        raise ReleaseRejected("VALUE_NOT_APPROVED")
                if col.mode == "generalize" and cells[col.name].value not in dict(col.generalizations):
                    raise ReleaseRejected("GENERALIZATION_UNDEFINED")
            records.append(Record(cells, row_aliases, state))
        all_columns.append(tuple(columns))
        all_records.append(tuple(records))
    scope = hmac.new(seed, b"release-scope-v1", hashlib.sha256).hexdigest()[:24]
    candidates = []
    for strategy in ("requested", "essential_only", "suppress_all"):
        artifacts, actions, loss = [], [], 0
        for i, ((document, _), columns, records) in enumerate(zip(bindings, all_columns, all_records)):
            budget.check()
            selected = tuple(c for c in columns if c.mode != "drop" and (strategy != "essential_only" or c.required or c.role == "key"))
            rows = []
            for row_index, record in enumerate(records):
                budget.check()
                values = []
                for fi, col in enumerate(columns):
                    budget.check()
                    cell = record.cells[col.name]
                    mode = col.mode if col in selected else "drop"
                    if strategy == "suppress_all":
                        mode = "suppress"
                    if cell.value and mode != "keep":
                        loc = cell.locator
                        actions.append(Action(i, row_index, fi, mode,
                            tuple(sorted(evidence_at.get((loc.document_id, loc.start, loc.end), ()))), (loc,)))
                    loss += {"keep": 0, "alias": 1, "generalize": 1, "drop": 2, "suppress": 3}[mode]
                    if mode == "alias":
                        values.append(record.aliases.get(col.name, ""))
                    elif mode == "keep":
                        values.append(cell.value)
                    elif mode == "generalize":
                        values.append(dict(col.generalizations)[cell.value])
                if strategy != "suppress_all":
                    rows.append(tuple(values + [record.state]))
            # Explicitly account for unconfigured CSV columns being dropped.
            if document.snapshot.format == "csv":
                headers = document.csv_rows[0]
                declared = {c.name for c in columns}
                for block in document.blocks:
                    if block.row and headers[block.column] not in declared and headers[block.column] != bindings[i][1].state_field and block.text:
                        budget.check()
                        loc = document.locate(block, 0, len(block.text))
                        actions.append(Action(i, block.row - 1, None, "drop", (), (loc,)))
                        loss += 2
            if len(actions) > 100_000:
                raise ReleaseRejected("ACTION_LIMIT")
            output_format = "csv" if document.snapshot.format in {"docx", "pdf"} else document.snapshot.format
            suffix = {"csv": ".csv", "txt": ".txt", "markdown": ".md"}[output_format]
            filename = "DOC_" + hmac.new(seed, f"document:{i}".encode(), hashlib.sha256).hexdigest()[:24] + suffix
            payload = serialize_rows(output_format, selected, rows)
            artifacts.append(Artifact(i, filename, output_format, selected, tuple(rows), payload))
        candidates.append(Candidate(strategy, tuple(artifacts), tuple(actions), loss))
    plan = ReleasePlan(scope, graph, analysis, profile, bindings, tuple(all_records), tuple(all_columns),
                       tuple(sorted(protected)), tuple(sorted(strict_terms)), tuple(candidates), None)
    checked = []
    for candidate in candidates:
        budget.check()
        try:
            verify_candidate(plan, candidate)
            checked.append(replace(candidate, feasible=True))
        except ReleaseRejected as exc:
            checked.append(replace(candidate, reason=exc.reason))
    valid = [i for i, c in enumerate(checked) if c.feasible]
    return replace(plan, candidates=tuple(checked), selected=min(valid, key=lambda i: (checked[i].loss, i)) if valid else None)


def public_plan(plan):
    plan.graph._check(plan.analysis)
    return {"schema_version": "d3-plan-1", "scope_id": plan.scope_id,
            "input_projections": [{"document_index": i, "format": d.snapshot.format,
                "source_view": t.source_view, "table_index": t.table_index, "pages": list(t.pages),
                "parsing_status": d.coverage.status.value, "unprocessed": list(d.coverage.unprocessed)}
                for i, (d, t) in enumerate(plan.bindings)],
            "status": "validated" if plan.selected is not None else "rejected", "privacy_verified": False,
            "task": plan.profile.task, "selected": plan.selected,
            "candidates": [{"strategy": c.strategy, "feasible": c.feasible, "reason": c.reason,
                "loss_units": c.loss, "documents": len(c.artifacts), "rows": sum(len(a.rows) for a in c.artifacts),
                "artifacts": [{"filename": a.filename, "format": a.format, "rows": len(a.rows),
                    "fields": [{"name": col.output_name, "mode": col.mode} for col in a.columns]} for a in c.artifacts],
                "action_counts": {kind: sum(a.kind == kind for a in c.actions) for kind in ("alias", "generalize", "drop", "suppress")}}
                for c in plan.candidates]}
