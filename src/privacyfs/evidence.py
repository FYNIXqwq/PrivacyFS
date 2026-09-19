"""D2 directed, source-backed risk witnesses over explicit record templates.

Internal graph state is sensitive. Only public_graph_report is a shareable view.
No function here transforms or certifies source files.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .documents.models import Budget, Limits, ProcessingStopped
from .entities import EntityResolver, StaleAnalysisError, opaque
from .normalization import normalize_with_map
from .relations import AssertionState, extract_records

RULE_VERSION = "d2-explicit-graph-v1"
ATTACK_VERSION = "d2-directed-key-linkage-v1"


@dataclass(frozen=True)
class GraphLimits:
    max_records: int = 10_000
    max_assertions: int = 30_000
    max_occurrences: int = 30_000
    max_witnesses: int = 1_000
    max_steps: int = 200_000
    max_depth: int = 6
    max_seconds: float = 10.0

    def __post_init__(self):
        Limits(max_seconds=self.max_seconds)
        for key, value in vars(self).items():
            if key != "max_seconds" and (type(value) is not int or value < 1):
                raise ValueError("invalid graph limit")


class WorkBudget:
    def __init__(self, limits, cancel):
        self.limits = limits
        self.clock = Budget(Limits(max_seconds=limits.max_seconds), cancel)
        self.steps = 0

    def check(self):
        self.clock.check()
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise ProcessingStopped("GRAPH_WORK_LIMIT")


@dataclass(frozen=True)
class RawAssertion:
    assertion_id: str
    source: str
    target: str | None
    kind: str
    field_name: str = field(repr=False)
    value: str = field(repr=False)
    state: AssertionState
    locators: tuple = field(repr=False)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source: str
    target: str | None
    kind: str
    field_name: str = field(repr=False)
    value: str = field(repr=False)
    state: AssertionState
    assertion_ids: tuple[str, ...]


@dataclass(frozen=True)
class CutCandidate:
    evidence_id: str
    breaks_witness: bool
    blocks_endpoint_route: bool


@dataclass(frozen=True)
class EntityCandidate:
    candidate_id: str
    entity_ids: tuple[str, ...]
    state: str


@dataclass(frozen=True)
class RiskWitness:
    witness_id: str
    rule: str
    subject_id: str
    endpoint_id: str
    evidence_ids: tuple[str, ...]
    support: int | None = None
    population: int | None = None
    cuts: tuple[CutCandidate, ...] = ()


@dataclass(frozen=True)
class GraphAnalysis:
    graph_id: str
    version: int
    witnesses: tuple[RiskWitness, ...]
    evidence: tuple[Evidence, ...] = field(repr=False)
    candidates: tuple[EntityCandidate, ...]
    status: str = "complete"


class EvidenceGraph:
    """One DocumentSession and one frozen set of document revisions per graph.

    Graph and corrections are in-memory; close invalidates reports and clears
    owned values. Callers must close the DocumentSession separately.
    """
    def __init__(self, bindings, *, limits=None, cancel=None):
        self.graph_id = opaque("GRAPH")
        self.limits = limits or GraphLimits()
        bindings = tuple(bindings)
        if not bindings or len(bindings) > 1000:
            raise ValueError("invalid graph documents")
        workspaces = {d.snapshot.workspace_id for d, _ in bindings}
        ids = [d.snapshot.document_id for d, _ in bindings]
        if len(workspaces) != 1 or len(ids) != len(set(ids)):
            raise ValueError("mixed workspaces or duplicate document revisions")
        self.resolver = EntityResolver(next(iter(workspaces)))
        self._documents = {d.snapshot.document_id: d for d, _ in bindings}
        self._templates = {d.snapshot.document_id: t for d, t in bindings}
        self._quasi_fields = {f.name for _, t in bindings for f in t.fields if f.role == "quasi"}
        self._cancel = (cancel, tuple(d._cancel for d, _ in bindings))
        self._raw = {}
        self._raw_by_document = defaultdict(list)
        self._closed = False
        self._evidence_ids = {}
        self._candidate_ids = {}
        self.record_count = 0
        self._analysis = None
        budget = self._budget()
        try:
            for document, template in bindings:
                for cells, quoted in extract_records(document, template, budget):
                    budget.check()
                    if any(len(cell.value) > 65_536 for cell in cells.values()):
                        raise ProcessingStopped("FIELD_VALUE_LIMIT")
                    self.record_count += 1
                    if self.record_count > self.limits.max_records:
                        raise ProcessingStopped("RECORD_LIMIT")
                    state = template.default_state
                    if template.state_field:
                        try:
                            state = AssertionState(cells[template.state_field].value)
                        except ValueError:
                            state = AssertionState.UNKNOWN
                    if quoted:
                        state = AssertionState.QUOTED
                    keys = {}
                    for spec in template.fields:
                        if spec.role == "key":
                            cell = cells[spec.name]
                            if not cell.value or len(cell.value) > 1024:
                                raise ProcessingStopped("INVALID_EXPLICIT_KEY")
                            if len(self.resolver.occurrences) >= self.limits.max_occurrences:
                                raise ProcessingStopped("OCCURRENCE_LIMIT")
                            keys[spec.name] = self.resolver.add(spec.entity_type, spec.namespace, cell.value, cell.locator)
                    primary = keys[template.primary]
                    for spec in template.fields:
                        budget.check()
                        if spec.name == template.primary or not cells[spec.name].value:
                            continue
                        if len(self._raw) >= self.limits.max_assertions:
                            raise ProcessingStopped("ASSERTION_LIMIT")
                        current_state = state
                        if spec.values and cells[spec.name].value not in spec.values:
                            current_state = AssertionState.UNKNOWN
                        locations = [cells[template.primary].locator, cells[spec.name].locator]
                        if template.state_field and cells[template.state_field].locator is not None:
                            locations.append(cells[template.state_field].locator)
                        raw = RawAssertion(opaque("ASSERTION"), primary, keys.get(spec.name),
                            "relation" if spec.role == "key" else spec.role, spec.name,
                            "" if spec.role == "key" else cells[spec.name].value,
                            current_state, tuple(locations))
                        self._raw[raw.assertion_id] = raw
                        self._raw_by_document[document.snapshot.document_id].append(raw)
            self.resolver.seal()
            budget.check()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *args):
        self.close()

    def _budget(self):
        # The graph never relaxes an inherited document processing timeout.
        from dataclasses import replace
        seconds = min([self.limits.max_seconds] + [d._limits.max_seconds for d in self._documents.values() if d._limits])
        return WorkBudget(replace(self.limits, max_seconds=seconds), self._cancel)

    def _check(self, analysis=None):
        if self._closed:
            raise StaleAnalysisError("graph is closed")
        for document in self._documents.values():
            _ = document.text
        if analysis is not None and (analysis.graph_id != self.graph_id or analysis.version != self.resolver.version or analysis is not self._analysis):
            raise StaleAnalysisError("analysis no longer matches the graph revision")

    def _evidence(self, budget, only_document=None):
        groups = defaultdict(list)
        source_items = self._raw.values() if only_document is None else self._raw_by_document[only_document]
        for raw in source_items:
            budget.check()
            if raw.assertion_id in self.resolver.rejected_assertions:
                continue
            if only_document is not None and raw.locators[0].document_id != only_document:
                continue
            source = self.resolver.assignments[raw.source]
            target = self.resolver.assignments[raw.target] if raw.target else None
            if any(self.resolver.status.get(e) == "rejected" for e in (source, target) if e):
                continue
            # Key edges and identity mappings have fixed semantic roles; source
            # header spelling does not create independent corroboration.
            semantic_field = "" if raw.kind in {"relation", "identity"} else raw.field_name
            groups[(source, target, raw.kind, semantic_field, raw.value, raw.state)].append(raw.assertion_id)
        result = []
        for key, sources in groups.items():
            budget.check()
            # IDs are scoped opaque tokens, never hashes of guessable values.
            token = self._evidence_ids.setdefault(key, opaque("EVIDENCE"))
            result.append(Evidence(token, *key, tuple(sorted(sources))))
        return result

    def analyze(self, *, quasi_fields=(), max_support=2):
        self._check()
        self._analysis = None
        if not isinstance(quasi_fields, tuple) or len(set(quasi_fields)) != len(quasi_fields) or len(quasi_fields) > 8 or any(not isinstance(f, str) or not f for f in quasi_fields):
            raise ValueError("invalid quasi identifier fields")
        if quasi_fields and len(quasi_fields) < 2:
            raise ValueError("at least two quasi fields required")
        if not set(quasi_fields) <= self._quasi_fields:
            raise ValueError("unknown quasi identifier field")
        if type(max_support) is not int or max_support < 1:
            raise ValueError("invalid support threshold")
        budget = self._budget()
        evidence = self._evidence(budget)
        witnesses = self._risks(evidence, quasi_fields, max_support, budget)
        labels = defaultdict(set)
        for item in evidence:
            budget.check()
            if item.kind in {"identity", "label"} and item.state == AssertionState.POSITIVE:
                occurrence = self.resolver.occurrences[self._raw[item.assertion_ids[0]].source]
                label = normalize_with_map(item.value, check=budget.check, max_chars=65_536).text
                labels[(occurrence.entity_type, occurrence.namespace, label)].add(item.source)
        groups = sorted({tuple(sorted(group)) for group in labels.values() if len(group) > 1})
        candidates = tuple(EntityCandidate(self._candidate_ids.setdefault(group, opaque("CANDIDATE")), group,
            "rejected" if frozenset(group) in self.resolver.rejected_candidates else "candidate") for group in groups)
        budget.check()
        analysis = GraphAnalysis(self.graph_id, self.resolver.version, tuple(witnesses), tuple(evidence), candidates)
        self._analysis = analysis
        return analysis

    def _risks(self, evidence, quasi_fields, max_support, budget):
        denied = {(e.source, e.target, e.kind, e.field_name, e.value)
                  for e in evidence if e.state == AssertionState.NEGATIVE}
        active = [e for e in evidence if e.state == AssertionState.POSITIVE
                  and (e.source, e.target, e.kind, e.field_name, e.value) not in denied]
        by_id = {e.evidence_id: e for e in active}
        relations = defaultdict(list)
        identities, sensitive, quasi = [], defaultdict(list), defaultdict(lambda: defaultdict(list))
        for e in active:
            budget.check()
            if e.kind == "relation":
                relations[e.source].append(e)
            elif e.kind == "identity":
                identities.append(e)
            elif e.kind == "sensitive":
                sensitive[e.source].append(e)
            elif e.kind == "quasi":
                quasi[e.source][e.field_name].append(e)

        def paths(start, excluded=frozenset()):
            found, queue = {start: ()}, deque([start])
            while queue:
                node = queue.popleft()
                for edge in sorted(relations[node], key=lambda e: e.evidence_id):
                    budget.check()
                    if edge.evidence_id in excluded or edge.target in found:
                        continue
                    if len(found[node]) >= self.limits.max_depth:
                        raise ProcessingStopped("GRAPH_DEPTH_LIMIT")
                    found[edge.target] = found[node] + (edge.evidence_id,)
                    queue.append(edge.target)
            return found

        witnesses = []

        def append(rule, source, endpoint, chain, support=None, population=None):
            budget.check()
            if len(witnesses) >= self.limits.max_witnesses:
                raise ProcessingStopped("WITNESS_LIMIT")
            cuts = []
            for token in chain:
                budget.check()
                # Check alternative directed routes after removing the ENTIRE
                # deduplicated fact, including its copies. Not a byte edit plan.
                if rule != "numbered_join" or token in (chain[0], chain[-1]):
                    blocked = True
                else:
                    blocked = by_id[endpoint].source not in paths(source, frozenset({token}))
                cuts.append(CutCandidate(token, True, blocked))
            witnesses.append(RiskWitness(opaque("WITNESS"), rule, source, endpoint,
                                         tuple(chain), support, population, tuple(cuts)))

        for identity in identities:
            append("identity_mapping", identity.source, identity.evidence_id, (identity.evidence_id,))
            for node, chain in paths(identity.source).items():
                for fact in sensitive[node]:
                    append("numbered_join", identity.source, fact.evidence_id,
                           (identity.evidence_id, *chain, fact.evidence_id))

        if quasi_fields:
            groups = defaultdict(list)
            for entity, fields in quasi.items():
                budget.check()
                # Conflicting values cannot establish a unique quasi profile.
                if any(name not in fields or len({e.value for e in fields[name]}) != 1 for name in quasi_fields):
                    continue
                representative = self.resolver.occurrences[self._raw[fields[quasi_fields[0]][0].assertion_ids[0]].source]
                signature = (representative.entity_type, representative.namespace,
                             tuple(fields[name][0].value for name in quasi_fields))
                groups[signature].append(entity)
            populations = defaultdict(int)
            for signature, entities in groups.items():
                populations[signature[:2]] += len(entities)
            for signature, entities in groups.items():
                if len(entities) > max_support:
                    continue
                for entity in entities:
                    # This rule reports potential singling-out in the modeled
                    # cohort; it does not assert real-world identity or linkage.
                    chain = tuple(quasi[entity][name][0].evidence_id for name in quasi_fields)
                    append("quasi_combination", entity, chain[-1], chain, len(entities), populations[signature[:2]])
        return witnesses

    def per_file_baseline(self, *, quasi_fields=(), max_support=2):
        """Restrict evidence to one file while holding entity corrections fixed."""
        self._check()
        # This is an evidence-visibility baseline, not an independent resolver.
        # At revision zero resolution consists solely of explicit exact keys.
        result = set()
        budget = self._budget()
        for doc_id in self._documents:
            evidence = self._evidence(budget, only_document=doc_id)
            for witness in self._risks(evidence, quasi_fields, max_support, budget):
                result.add((witness.rule, witness.subject_id, witness.endpoint_id))
        return result

    def local_evidence(self, analysis, evidence_id):
        """Privileged local API. Contains raw values and SourceLocators."""
        self._check(analysis)
        evidence = next((e for e in analysis.evidence if e.evidence_id == evidence_id), None)
        if evidence is None:
            raise ValueError("unknown evidence")
        sources = []
        for assertion_id in evidence.assertion_ids:
            raw = self._raw[assertion_id]
            sources.append({"assertion_id": assertion_id, "occurrences": (raw.source, raw.target),
                "locations": raw.locators,
                "source_text": tuple(self._documents[loc.document_id].resolve(loc) for loc in raw.locators)})
        return {"evidence": evidence, "sources": sources, "corrections": tuple(self.resolver.corrections)}

    def reject_evidence(self, analysis, evidence_id):
        self._check(analysis)
        item = next((e for e in analysis.evidence if e.evidence_id == evidence_id), None)
        if item is None:
            raise ValueError("unknown evidence")
        # Reject all copies of the selected modeled fact in one revision.
        self.resolver._check(self.resolver.workspace_id, analysis.version)
        self.resolver.rejected_assertions.update(item.assertion_ids)
        return self.resolver._record("reject_evidence", item.assertion_ids)

    def reject_candidate(self, analysis, candidate_id):
        """Reject a proposed co-reference without suppressing either entity."""
        self._check(analysis)
        candidate = next((c for c in analysis.candidates if c.candidate_id == candidate_id), None)
        if candidate is None:
            raise ValueError("unknown entity candidate")
        self.resolver._check(self.resolver.workspace_id, analysis.version)
        self.resolver.rejected_candidates.add(frozenset(candidate.entity_ids))
        return self.resolver._record("reject_candidate", candidate.entity_ids)

    def close(self):
        self._closed = True
        self.resolver.clear()
        self._raw.clear()
        self._raw_by_document.clear()
        self._documents.clear()
        self._templates.clear()
        self._evidence_ids.clear()
        self._candidate_ids.clear()
        self._quasi_fields.clear()
        self._analysis = None


def public_graph_report(graph, analysis):
    """Safe IDs only; field names, paths, values, locators and hashes excluded."""
    graph._check(analysis)
    baseline = graph.per_file_baseline()
    signs = defaultdict(set)
    for e in analysis.evidence:
        signs[e.source, e.target, e.kind, e.field_name, e.value].add(e.state)
    return {
        "schema_version": "d2-evidence-1", "status": analysis.status,
        "scope": "explicit_configured_fields", "privacy_verified": False,
        "graph_id": graph.graph_id, "revision": analysis.version,
        "rule_version": RULE_VERSION, "attack_version": ATTACK_VERSION,
        "document_count": len(graph._documents), "record_count": graph.record_count,
        "entity_count": len(set(graph.resolver.assignments.values())),
        "evidence_count": len(analysis.evidence), "risk_count": len(analysis.witnesses),
        "conflicting_fact_count": sum({AssertionState.POSITIVE, AssertionState.NEGATIVE} <= states for states in signs.values()),
        "candidate_groups": [{"candidate_id": c.candidate_id, "entity_ids": list(c.entity_ids), "state": c.state} for c in analysis.candidates],
        "correction_ids": [c.correction_id for c in graph.resolver.corrections],
        "entity_states": {e: graph.resolver.status[e] for e in sorted(set(graph.resolver.assignments.values()))},
        "assertion_states": {s.value: sum(e.state == s for e in analysis.evidence) for s in AssertionState},
        "incremental_numbered_joins": sum(w.rule == "numbered_join" and (w.rule, w.subject_id, w.endpoint_id) not in baseline for w in analysis.witnesses),
        "witnesses": [{"witness_id": w.witness_id, "rule": w.rule, "subject_id": w.subject_id,
            "endpoint_id": w.endpoint_id, "evidence_ids": list(w.evidence_ids),
            "modeled_support": w.support, "modeled_population": w.population,
            "reduction": "shortest_directed_path_or_configured_field_set",
            "cuts": [{"evidence_id": c.evidence_id, "breaks_witness": c.breaks_witness,
                "blocks_endpoint_route": c.blocks_endpoint_route} for c in w.cuts]} for w in analysis.witnesses],
    }
