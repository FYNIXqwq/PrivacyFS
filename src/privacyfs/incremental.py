"""Content/version caches and conservative dependency-component recomputation."""
from __future__ import annotations

from collections import defaultdict, Counter
from dataclasses import asdict, replace
from itertools import combinations
import hashlib
import hmac
from pathlib import Path
import time

from .documents import DocumentSession
from .documents.models import (Budget, ContentBlock, DocumentIR, Limits, ParseCoverage, ParseStatus,
                               ProcessingStopped, SourceLocator)
from .entities import EntityResolver, Occurrence, Correction
from .evidence import (EvidenceGraph, GraphAnalysis, GraphLimits, RawAssertion, RULE_VERSION,
                       ATTACK_VERSION, Evidence, EntityCandidate, RiskWitness, CutCandidate)
from .normalization import NORMALIZATION_VERSION, NormalizedText, normalize_with_map
from .relations import AssertionState, EntityType, extract_records, load_relation_config
from .release import canonical, control_bytes
from .source_map import OffsetMap
from .state_store import WorkspaceStore
from .verifier import ReleaseRejected
from .task_profiles import load_task_profile

CACHE_VERSION = "d5-cache-v2"


def dump_map(mapping):
    return [[r.view_end - r.view_start, r.source_start, r.source_end, r.linear] for r in mapping.runs]


def load_map(rows):
    mapping = OffsetMap()
    for length, start, end, linear in rows:
        mapping.append(length, start, end, linear=linear)
    return mapping


def dump_document(doc):
    return {"text": doc.text, "encoding": doc.encoding, "bom": doc.bom_bytes, "newline": doc.newline,
            "rows": doc.csv_rows, "coverage": asdict(doc.coverage),
            "blocks": [{"id": b.block_id, "kind": b.kind, "text": b.text,
                        "map": dump_map(b.source_map), "normalized": b.normalized.text,
                        "norm_map": dump_map(b.normalized.mapping), "row": b.row, "column": b.column,
                        "component": b.component_index, "part": b.part, "page": b.page,
                        "paragraph": b.paragraph, "table": b.table} for b in doc.blocks]}


def restore_document(snapshot, value, session):
    coverage = ParseCoverage(**{**value["coverage"], "status": ParseStatus(value["coverage"]["status"])})
    blocks = [ContentBlock(b["id"], b["kind"], b["text"], load_map(b["map"]),
              NormalizedText(b["normalized"], load_map(b["norm_map"])), snapshot._lease,
              b["row"], b["column"], b["component"], part=b.get("part"), page=b.get("page"),
              paragraph=b.get("paragraph"), table=b.get("table")) for b in value["blocks"]]
    doc = DocumentIR(snapshot, coverage, value["text"], blocks, value["encoding"], value["bom"],
                     value["newline"], value["rows"], snapshot._lease, _limits=session.limits, _cancel=session.cancel)
    return doc


class WorkspaceDocuments(DocumentSession):
    def __init__(self, record, store, *, cancel=None):
        super().__init__(record["request"]["root"], cancel=cancel)
        self.workspace_id = record["workspace_id"]
        self._secret = bytes.fromhex(store.token("document-session", self.workspace_id))

    def _snapshot(self, path, format, payload=None, reason=None, size=0):
        result = super()._snapshot(path, format, payload, reason, size)
        if payload is None:
            return result
        revision = "REVISION_" + hmac.new(self._secret, canonical([result.document_id, result.content_digest, format]), hashlib.sha256).hexdigest()[:24]
        self._payloads[revision] = self._payloads.pop(result.revision_id)
        self._snapshots.pop(result.revision_id)
        result = replace(result, revision_id=revision)
        self._snapshots[revision] = result
        return result


class WorkspaceGraph(EvidenceGraph):
    def set_tokens(self, token, graph_id):
        self.token = token
        self.graph_id = graph_id

    def _evidence(self, budget, only_document=None):
        result = super()._evidence(budget, only_document)
        for key in self._evidence_ids:
            self._evidence_ids[key] = "EVIDENCE_" + self.token("evidence", key)[:24]
        return [replace(e, evidence_id=self._evidence_ids[e.source, e.target, e.kind, e.field_name, e.value, e.state]) for e in result]

    def analyze(self, **kwargs):
        original = super().analyze(**kwargs)
        candidates = tuple(replace(c, candidate_id="CANDIDATE_" + self.token("candidate", c.entity_ids)[:24]) for c in original.candidates)
        witnesses = tuple(sorted((replace(w, witness_id="WITNESS_" + self.token("witness", w.rule, w.subject_id, w.endpoint_id, w.evidence_ids)[:24]) for w in original.witnesses), key=lambda w: w.witness_id))
        result = replace(original, candidates=candidates, witnesses=witnesses,
                         evidence=tuple(sorted(original.evidence, key=lambda e: e.evidence_id)))
        self._analysis = result
        return result


def rekey_graph(graph):
    resolver = graph.resolver
    occurrences, occurrence_ids, entities = {}, {}, {}
    for oid, occurrence in resolver.occurrences.items():
        loc = occurrence.locator
        new = "OCCURRENCE_" + graph.token("occurrence", asdict(loc), occurrence.entity_type, occurrence.namespace)[:24]
        occurrence_ids[oid] = new
        occurrences[new] = replace(occurrence, occurrence_id=new)
        entities[resolver.assignments[oid]] = "ENTITY_" + graph.token("entity", occurrence.entity_type, occurrence.namespace, occurrence.value)[:24]
    resolver.assignments = {occurrence_ids[o]: entities[e] for o, e in resolver.assignments.items()}
    resolver.status = {entities[e]: status for e, status in resolver.status.items()}
    resolver.occurrences = occurrences
    resolver._keys = {key: entities[e] for key, e in resolver._keys.items()}
    raw = {}
    for item in graph._raw.values():
        aid = "ASSERTION_" + graph.token("assertion", [asdict(loc) for loc in item.locators], item.kind, item.field_name, item.value, item.state)[:24]
        raw[aid] = replace(item, assertion_id=aid, source=occurrence_ids[item.source], target=occurrence_ids.get(item.target))
    graph._raw = raw
    graph._raw_by_document = defaultdict(list)
    for item in raw.values():
        graph._raw_by_document[item.locators[0].document_id].append(item)


def dump_graph(graph):
    resolver = graph.resolver
    return {"occurrences": [asdict(o) for o in resolver.occurrences.values()], "assignments": resolver.assignments,
            "status": resolver.status, "raw": [asdict(r) for r in graph._raw.values()],
            "rejected_assertions": sorted(resolver.rejected_assertions),
            "rejected_candidates": [sorted(c) for c in resolver.rejected_candidates],
            "corrections": [asdict(c) for c in resolver.corrections], "records": graph.record_count}


def analyze_component(graph):
    baseline = graph.analyze()
    witnesses = {w.witness_id: w for w in baseline.witnesses}
    fields = tuple(sorted(graph._quasi_fields))
    if len(fields) > 8:
        raise ProcessingStopped("QUASI_COMBINATION_LIMIT")
    budget = graph._budget()
    for size in range(2, len(fields) + 1):
        for subset in combinations(fields, size):
            budget.check()
            for witness in graph.analyze(quasi_fields=subset).witnesses:
                witnesses[witness.witness_id] = witness
    result = replace(baseline, witnesses=tuple(sorted(witnesses.values(), key=lambda w: w.witness_id)))
    graph._analysis = result
    return result


def restore_analysis(data, graph):
    witnesses = tuple(RiskWitness(**{**w, "evidence_ids": tuple(w["evidence_ids"]),
                                     "cuts": tuple(CutCandidate(**c) for c in w["cuts"])}) for w in data["witnesses"])
    evidence = tuple(Evidence(**{**e, "assertion_ids": tuple(e["assertion_ids"]), "state": AssertionState(e["state"])}) for e in data["evidence"])
    candidates = tuple(EntityCandidate(**{**c, "entity_ids": tuple(c["entity_ids"])}) for c in data["candidates"])
    result = GraphAnalysis(graph.graph_id, graph.resolver.version, witnesses, evidence, candidates)
    graph._analysis = result
    return result


def restore_graph(bindings, data, token, graph_id, revision, cancel=None):
    graph = WorkspaceGraph.__new__(WorkspaceGraph)
    graph.set_tokens(token, graph_id)
    graph.limits = GraphLimits()
    graph._documents = {d.snapshot.document_id: d for d, _ in bindings}
    graph._templates = {d.snapshot.document_id: t for d, t in bindings}
    graph._quasi_fields = {f.name for _, t in bindings for f in t.fields if f.role == "quasi"}
    graph._cancel = (cancel, tuple(d._cancel for d, _ in bindings))
    graph._closed = False
    graph._analysis = None
    graph._evidence_ids, graph._candidate_ids = {}, {}
    graph.resolver = EntityResolver(bindings[0][0].snapshot.workspace_id)
    resolver = graph.resolver
    resolver.occurrences = {o["occurrence_id"]: Occurrence(**{**o, "entity_type": EntityType(o["entity_type"]), "locator": SourceLocator(**o["locator"])}) for o in data["occurrences"]}
    resolver.assignments, resolver.status = dict(data["assignments"]), dict(data["status"])
    resolver.rejected_assertions = set(data["rejected_assertions"])
    resolver.rejected_candidates = {frozenset(c) for c in data["rejected_candidates"]}
    resolver.corrections = [Correction(**{**c, "references": tuple(c["references"])}) for c in data["corrections"]]
    resolver.version = revision
    resolver.seal()
    graph._raw = {r["assertion_id"]: RawAssertion(**{**r, "state": AssertionState(r["state"]), "locators": tuple(SourceLocator(**loc) for loc in r["locators"])}) for r in data["raw"]}
    graph._raw_by_document = defaultdict(list)
    for item in graph._raw.values():
        graph._raw_by_document[item.locators[0].document_id].append(item)
    graph.record_count = data["records"]
    return graph


def apply_corrections(graph, events):
    for event in events:
        if not event["active"] or event.get("stale"):
            continue
        action, targets = event["action"], event["targets"]
        kwargs = {"workspace_id": graph.resolver.workspace_id, "expected_version": graph.resolver.version}
        try:
            if action == "confirm":
                entities = {graph.resolver.assignments[o] for o in targets}
                graph.resolver.confirm(entities, **kwargs)
            elif action == "split":
                entity = graph.resolver.assignments[targets[0]]
                if any(graph.resolver.assignments[o] != entity for o in targets):
                    raise ValueError()
                graph.resolver.split(entity, targets[1:], **kwargs)
                old_id = graph.resolver.corrections[-1].references[1]
                stable_id = "ENTITY_" + graph.token("split", event["id"])[:24]
                graph.resolver.assignments = {o: stable_id if e == old_id else e for o, e in graph.resolver.assignments.items()}
                graph.resolver.status[stable_id] = graph.resolver.status.pop(old_id)
                graph.resolver.corrections[-1] = replace(graph.resolver.corrections[-1], references=tuple(stable_id if x == old_id else x for x in graph.resolver.corrections[-1].references))
            elif action == "reject_entity":
                graph.resolver.reject_entity(graph.resolver.assignments[targets[0]], **kwargs)
            elif action in {"reject_evidence", "reject_candidate"}:
                analysis = graph.analyze()
                if action == "reject_evidence":
                    graph.reject_evidence(analysis, targets[0])
                else:
                    graph.reject_candidate(analysis, targets[0])
            else:
                raise ValueError()
            graph.resolver.corrections[-1] = replace(graph.resolver.corrections[-1], correction_id=event["id"])
        except (KeyError, ValueError):
            raise ReleaseRejected("CORRECTION_DEPENDENCY_CHANGED") from None


def dependency_components(bindings, events, budget):
    parents = list(range(len(bindings)))
    index = {d.snapshot.document_id: i for i, (d, _) in enumerate(bindings)}
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    def union(a, b):
        parents[find(b)] = find(a)
    seen = {}
    for i, (document, template) in enumerate(bindings):
        primary = next(f for f in template.fields if f.name == template.primary)
        for cells, _ in extract_records(document, template, budget):
            for spec in template.fields:
                budget.check()
                value = cells[spec.name].value
                if spec.role == "key":
                    key = ("key", spec.entity_type, spec.namespace, value)
                elif spec.role in {"identity", "label"} and value:
                    key = ("label", primary.entity_type, primary.namespace, normalize_with_map(value, check=budget.check).text)
                elif spec.role == "quasi":
                    key = ("cohort", primary.entity_type, primary.namespace)
                else:
                    continue
                if key in seen:
                    union(i, seen[key])
                else:
                    seen[key] = i
    for event in events:
        if event["active"] and not event.get("stale"):
            locations = [index[a["document_id"]] for a in event["anchors"] if a["document_id"] in index]
            for other in locations[1:]:
                union(locations[0], other)
    groups = defaultdict(list)
    for i in range(len(bindings)):
        groups[find(i)].append(i)
    return list(groups.values())


class WorkspaceView:
    """Refresh under the ReleaseStore lock. Close invalidates all local handles."""
    def __init__(self, releases, workspace_id, *, cancel=None, force=False, persist=True):
        self.store = WorkspaceStore(releases)
        self.record = self.store.load(workspace_id)
        self.cancel, self.force, self.persist = cancel, force, persist
        self.session, self.graph = None, None
        self.bindings, self.analysis, self.stats = [], None, Counter()
        self.components = []

    def __enter__(self):
        try:
            self.refresh()
            return self
        except BaseException as exc:
            if self.persist and self.record["status"] != "purging" and not isinstance(exc, KeyboardInterrupt) and not (isinstance(exc, ProcessingStopped) and exc.reason == "CANCELLED"):
                if self.record["status"] != "failed":
                    self.record["status"] = "failed"
                    self.record["revision"] += 1
                    self.store.save(self.record)
                    self.store.invalidate_plans(self.record)
            self.close()
            raise

    def __exit__(self, *args):
        self.close()

    def refresh(self):
        started = time.perf_counter()
        record, workspace_id = self.record, self.record["workspace_id"]
        if record["status"] == "purging":
            raise ReleaseRejected("WORKSPACE_PURGE_PENDING")
        budget = Budget(Limits(max_seconds=60), self.cancel)
        request = record["request"]
        before = {k: control_bytes(request[k]) for k in ("relations", "profile")}
        settings = load_relation_config(Path(request["relations"]))
        load_task_profile(Path(request["profile"]))
        controls = {k: self.store.token(workspace_id, payload.hex()) for k, payload in before.items()}
        if any(control_bytes(request[k]) != value for k, value in before.items()):
            raise ReleaseRejected("CONTROL_CHANGED")
        self.session = WorkspaceDocuments(record, self.store, cancel=self.cancel)
        files, chars, blocks = {}, 0, 0
        for path, template, encoding in settings:
            budget.check()
            snapshot = self.session.capture(path)
            if snapshot.capture_reason:
                raise ProcessingStopped(snapshot.capture_reason)
            cache = self.store.token(workspace_id, "parse", snapshot.content_digest, snapshot.format, encoding,
                                     CACHE_VERSION, NORMALIZATION_VERSION, asdict(self.session.limits))
            cached = None if self.force else self.store.cache_read(workspace_id, cache)
            if cached is None:
                document = self.session.parse(snapshot, encoding=encoding)
                if document.coverage.status != ParseStatus.COMPLETE and not (
                        document.snapshot.format in {"docx", "pdf"} and document.coverage.projection_complete):
                    raise ProcessingStopped(document.coverage.reason or "PARSING_INCOMPLETE")
                self.store.cache_write(workspace_id, cache, dump_document(document), replace_existing=self.force)
                self.stats["parsed_documents"] += 1
            else:
                document = restore_document(snapshot, cached, self.session)
                self.stats["parse_cache_hits"] += 1
            chars += len(document.text) + sum(len(b.text) + len(b.normalized.text) for b in document.blocks)
            blocks += len(document.blocks)
            if chars > self.session.limits.max_total_chars or blocks > self.session.limits.max_blocks:
                raise ProcessingStopped("TOTAL_TEXT_LIMIT")
            self.bindings.append((document, template))
            doc_id = snapshot.document_id
            if doc_id in files:
                raise ValueError("duplicate workspace document")
            files[doc_id] = {"path": path, "revision": snapshot.revision_id, "digest": snapshot.content_digest,
                             "cache": cache, "template": self.store.token(asdict(template), encoding,
                                 CACHE_VERSION, NORMALIZATION_VERSION, RULE_VERSION, ATTACK_VERSION)}
        changed = files != record["files"] or controls != record["controls"] or record["status"] != "ready"
        old = record["files"]
        events = []
        for doc_id in sorted(set(old) | set(files)):
            if doc_id not in old:
                events.append({"kind": "added", "document_id": doc_id})
            elif doc_id not in files:
                events.append({"kind": "removed", "document_id": doc_id})
            elif old[doc_id] != files[doc_id]:
                events.append({"kind": "changed", "document_id": doc_id})
        for removed in [e for e in events if e["kind"] == "removed"]:
            matches = [e for e in events if e["kind"] == "added" and files[e["document_id"]]["digest"] == old[removed["document_id"]]["digest"]]
            old_matches = [e for e in events if e["kind"] == "removed" and old[e["document_id"]]["digest"] == old[removed["document_id"]]["digest"]]
            if len(matches) == 1 and len(old_matches) == 1:
                removed["possible_rename_to"] = matches[0]["document_id"]
        for correction in record["corrections"]:
            if correction["active"] and not correction.get("stale"):
                if any(a["document_id"] not in files or files[a["document_id"]]["revision"] != a["revision"]
                       or files[a["document_id"]]["template"] != a["template"] for a in correction["anchors"]):
                    correction["stale"] = True
                    changed = True
        revision = record["revision"] + int(changed)
        active = [c for c in record["corrections"] if c["active"] and not c.get("stale")]
        groups = dependency_components(self.bindings, active, budget)
        keys = {}
        token = lambda *args: self.store.token(workspace_id, *args)
        for group in groups:
            budget.check()
            bindings = [self.bindings[i] for i in group]
            ids = {d.snapshot.document_id for d, _ in bindings}
            corrections = [c for c in active if any(a["document_id"] in ids for a in c["anchors"])]
            cache_key = token("graph", CACHE_VERSION, RULE_VERSION, ATTACK_VERSION, NORMALIZATION_VERSION,
                              sorted((i, files[i]["revision"], files[i]["template"]) for i in ids), corrections)
            data = None if self.force else self.store.cache_read(workspace_id, cache_key)
            graph_id = "GRAPH_" + cache_key[:24]
            if data is None:
                graph = WorkspaceGraph(bindings, cancel=self.cancel)
                graph.set_tokens(token, graph_id)
                rekey_graph(graph)
                apply_corrections(graph, corrections)
                graph.resolver.version = revision
                analysis = analyze_component(graph)
                self.store.cache_write(workspace_id, cache_key, {"graph": dump_graph(graph), "analysis": asdict(analysis)}, replace_existing=self.force)
                self.stats["rebuilt_components"] += 1
            else:
                graph = restore_graph(bindings, data["graph"], token, graph_id, revision, self.cancel)
                analysis = restore_analysis(data["analysis"], graph)
                self.stats["graph_cache_hits"] += 1
            # A cache hit restores both source graph and immutable risk result.
            self.components.append((graph, analysis))
            keys[token("component", sorted(ids))] = cache_key
        merged = {"occurrences": [], "assignments": {}, "status": {}, "raw": [], "rejected_assertions": [],
                  "rejected_candidates": [], "corrections": [], "records": 0}
        for graph, _ in self.components:
            value = dump_graph(graph)
            for key in merged:
                if isinstance(merged[key], list): merged[key].extend(value[key])
                elif isinstance(merged[key], dict): merged[key].update(value[key])
                else: merged[key] += value[key]
        if merged["records"] > 10_000 or len(merged["raw"]) > 30_000 or len(merged["occurrences"]) > 30_000:
            raise ProcessingStopped("WORKSPACE_GRAPH_LIMIT")
        self.graph = restore_graph(self.bindings, merged, token, "GRAPH_" + token("combined", revision, keys)[:24], revision, self.cancel)
        witnesses = tuple(sorted((w for _, a in self.components for w in a.witnesses), key=lambda w: w.witness_id))
        if len(witnesses) > 1000:
            raise ProcessingStopped("WITNESS_LIMIT")
        self.analysis = GraphAnalysis(self.graph.graph_id, revision, witnesses,
            tuple(sorted((e for _, a in self.components for e in a.evidence), key=lambda e: e.evidence_id)),
            tuple(sorted((c for _, a in self.components for c in a.candidates), key=lambda c: c.candidate_id)))
        self.graph._analysis = self.analysis
        budget.check()
        self.stats["documents"] = len(files)
        self.stats["components"] = len(keys)
        self.stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        if self.persist:
            index_changed = changed or keys != record["components"]
            record.update(files=files, controls=controls, components=keys, revision=revision, status="ready")
            record["events"] = (record["events"] + [{"revision": revision, **e} for e in events])[-1000:]
            if index_changed:
                self.store.save(record)
                self.store.invalidate_plans(record)
            if not record["keep_cache"]:
                self.store.prune(record, dry_run=False)
        else:
            record.update(files=files, controls=controls, revision=revision)

    def public(self):
        self.graph._check(self.analysis)
        signs = defaultdict(set)
        for e in self.analysis.evidence:
            signs[e.source, e.target, e.kind, e.field_name, e.value].add(e.state)
        conflicts = {key for key, states in signs.items() if {AssertionState.POSITIVE, AssertionState.NEGATIVE} <= states}
        entity_types = {self.graph.resolver.assignments[oid]: o.entity_type.value for oid, o in self.graph.resolver.occurrences.items()}
        return {"schema_version": "d4-workspace-report-1", "workspace_id": self.record["workspace_id"],
                "revision": self.record["revision"], "status": "needs_review" if any(c["active"] and c.get("stale") for c in self.record["corrections"]) else "ready",
                "privacy_verified": False, "source_checked": True, "metrics": dict(self.stats),
                "storage_protection": self.store.protector.name,
                "risk_count": len(self.analysis.witnesses),
                "assertion_states": dict(Counter(e.state.value for e in self.analysis.evidence)),
                "conflicting_fact_count": len(conflicts),
                "review_items": [{"evidence_id": e.evidence_id, "kind": e.kind, "state": e.state.value}
                    for e in self.analysis.evidence if e.state != AssertionState.POSITIVE or (e.source, e.target, e.kind, e.field_name, e.value) in conflicts],
                "changes": self.record["events"][-100:],
                "risks": [{"witness_id": w.witness_id, "rule": w.rule, "subject_id": w.subject_id,
                           "evidence_ids": list(w.evidence_ids), "support": w.support} for w in self.analysis.witnesses],
                "entities": [{"entity_id": e, "entity_type": entity_types[e], "state": self.graph.resolver.status[e]} for e in sorted(set(self.graph.resolver.assignments.values()))],
                "candidates": [asdict(c) for c in self.analysis.candidates],
                "corrections": [{k: c[k] for k in ("id", "action", "active", "stale")} for c in self.record["corrections"]]}

    def close(self):
        if self.graph:
            self.graph.close()
        for graph, _ in self.components:
            graph.close()
        for document, _ in self.bindings:
            document.clear()
        self.bindings.clear()
        if self.session:
            self.session.close()
