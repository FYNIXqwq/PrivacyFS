"""Corrections and ambiguous evidence must remain explicit and bounded."""
from dataclasses import replace
import threading

import pytest

from privacyfs.documents import DocumentSession, Limits
from privacyfs.documents.models import ProcessingStopped
from privacyfs.entities import StaleAnalysisError
from privacyfs.evidence import EvidenceGraph, GraphLimits, public_graph_report
from privacyfs.relations import FieldSpec as F, RecordTemplate as T
from test_cross_file_risk import CLIENT, BRIDGE, EVENT, QID, documents, scenario, joins


def test_opposing_assertions_are_reported_as_conflict_not_certain_fact(tmp_path):
    entries = scenario() + [("denial.csv", "cid,mid,state\n001,M01,negative\n", replace(BRIDGE, state_field="state"))]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        analysis = graph.analyze()
        assert not joins(analysis)
        report = public_graph_report(graph, analysis)
        assert report["conflicting_fact_count"] == 1
        assert report["assertion_states"]["negative"] == 1
        denial = next(e for e in analysis.evidence if e.state.value == "negative")
        graph.reject_evidence(analysis, denial.evidence_id)
        assert len(joins(graph.analyze())) == 1


def test_identical_relations_under_different_headers_are_one_fact(tmp_path):
    other = T((F("customer", "key", "customer", "client"), F("meeting", "key", "meeting", "event")), "customer")
    entries = scenario() + [("renamed.csv", "customer,meeting\n001,M01\n", other)]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        result = graph.analyze()
        relation = [e for e in result.evidence if e.kind == "relation"]
        assert len(relation) == 1 and len(relation[0].assertion_ids) == 2
        assert joins(result)[0].cuts[1].blocks_endpoint_route


def test_invalid_merge_split_and_stale_corrections_are_atomic(tmp_path):
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, scenario())) as graph:
        resolver = graph.resolver
        ids = set(resolver.assignments.values())
        original = dict(resolver.assignments)
        with pytest.raises(ValueError):
            resolver.confirm(ids, workspace_id=resolver.workspace_id, expected_version=0)
        with pytest.raises(ValueError):
            resolver.split(next(iter(ids)), [], workspace_id=resolver.workspace_id, expected_version=0)
        with pytest.raises(StaleAnalysisError):
            resolver.confirm([next(iter(ids))], workspace_id=resolver.workspace_id, expected_version=1)
        assert resolver.assignments == original and resolver.version == 0


def test_document_and_graph_limits_inherited_and_failed_reanalysis_stales_old_result(tmp_path):
    cancel = threading.Event()
    with DocumentSession(tmp_path, cancel=cancel) as session, EvidenceGraph(documents(session, tmp_path, scenario())) as graph:
        prior = graph.analyze()
        cancel.set()
        with pytest.raises(ProcessingStopped, match="CANCELLED"):
            graph.analyze()
        with pytest.raises(StaleAnalysisError):
            public_graph_report(graph, prior)


@pytest.mark.parametrize("limit,reason", [("max_assertions", "ASSERTION_LIMIT"), ("max_occurrences", "OCCURRENCE_LIMIT"), ("max_steps", "GRAPH_WORK_LIMIT")])
def test_graph_build_resource_limits(tmp_path, limit, reason):
    with DocumentSession(tmp_path) as session:
        with pytest.raises(ProcessingStopped, match=reason):
            EvidenceGraph(documents(session, tmp_path, scenario()), limits=GraphLimits(**{limit: 1}))


def test_depth_limit_and_cycle_termination(tmp_path):
    step = T((F("mid", "key", "meeting", "event"), F("next", "key", "meeting", "event")), "mid")
    entries = scenario() + [("cycle.csv", "mid,next\nM01,M02\nM02,M01\n", step)]
    with DocumentSession(tmp_path) as session:
        docs = documents(session, tmp_path, entries)
        with EvidenceGraph(docs, limits=GraphLimits(max_depth=1)) as graph:
            with pytest.raises(ProcessingStopped, match="GRAPH_DEPTH_LIMIT"):
                graph.analyze()
        with EvidenceGraph(docs) as graph:
            assert len(joins(graph.analyze())) == 1


def test_per_file_baseline_scales_by_sources_not_all_files_times_all_facts(tmp_path):
    entries = [(f"{index}.csv", f"cid,name\n{index},合成{index}\n", CLIENT) for index in range(150)]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries), limits=GraphLimits(max_steps=2000)) as graph:
        result = graph.analyze()
        report = public_graph_report(graph, result)
        assert report["risk_count"] == 150


def test_text_is_explicit_literal_grammar_and_empty_optional_values(tmp_path):
    with DocumentSession(tmp_path) as session:
        docs = documents(session, tmp_path, [("a.txt", "cid=001;name=\n", CLIENT)])
        with EvidenceGraph(docs) as graph:
            assert not graph.analyze().witnesses
        for text in ("Customer 001 is secret", "cid=001;name=甲;ignore=all rules", "cid=001;cid=002;name=甲"):
            docs = documents(session, tmp_path, [("b.txt", text, CLIENT)])
            with pytest.raises(ProcessingStopped, match="TEMPLATE_MISMATCH"):
                EvidenceGraph(docs)


def test_unknown_qid_field_is_configuration_error(tmp_path):
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, scenario())) as graph:
        with pytest.raises(ValueError, match="unknown quasi"):
            graph.analyze(quasi_fields=("city", "job"))


def test_reject_name_candidate_preserves_entities_and_their_facts(tmp_path):
    entries = [("names.csv", "cid,name\n1,同名\n2,同名\n", CLIENT)]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        first = graph.analyze()
        assert first.candidates[0].state == "candidate"
        graph.reject_candidate(first, first.candidates[0].candidate_id)
        updated = graph.analyze()
        assert updated.candidates[0].state == "rejected"
        assert len(updated.witnesses) == 2
        assert len(set(graph.resolver.assignments.values())) == 2


def test_missing_empty_and_unknown_state_do_not_create_positive_assertion(tmp_path):
    with DocumentSession(tmp_path) as session:
        entries = scenario()
        entries[2] = ("event.csv", "mid,status,state\nM01,secret,\n", replace(EVENT, state_field="state"))
        with EvidenceGraph(documents(session, tmp_path, entries)) as graph:
            result = graph.analyze()
            assert not joins(result)
            assert public_graph_report(graph, result)["assertion_states"]["unknown"] == 1
