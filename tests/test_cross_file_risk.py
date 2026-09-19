"""D2 observable invariants using synthetic documents only."""
from dataclasses import replace
import json
import subprocess
import sys
import threading

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.documents import DocumentSession, Limits
from privacyfs.documents.models import ClosedSessionError, ProcessingStopped
from privacyfs.entities import StaleAnalysisError
from privacyfs.evidence import EvidenceGraph, GraphLimits, public_graph_report
from privacyfs.relations import FieldSpec as F, RecordTemplate as T, load_relation_config


CLIENT = T((F("cid", "key", "customer", "client"), F("name", "identity")), "cid")
BRIDGE = T((F("cid", "key", "customer", "client"), F("mid", "key", "meeting", "event")), "cid")
EVENT = T((F("mid", "key", "meeting", "event"), F("status", "sensitive", values=("secret", "ended"))), "mid")
QID = T((F("cid", "key", "customer", "client"), F("city", "quasi"), F("job", "quasi")), "cid")


def documents(session, root, entries):
    result = []
    for path, text, template in entries:
        (root / path).write_text(text, encoding="utf-8")
        result.append((session.parse(session.capture(path)), template))
    return result


def scenario():
    return [("clients.csv", "cid,name\n001,示例企业甲\n", CLIENT),
            ("bridge.csv", "cid,mid\n001,M01\n", BRIDGE),
            ("event.md", "mid=M01;status=secret\n", EVENT)]


def joins(analysis):
    return [w for w in analysis.witnesses if w.rule == "numbered_join"]


def test_three_file_witness_replays_and_per_file_baseline_misses_it(tmp_path):
    with DocumentSession(tmp_path) as session:
        docs = documents(session, tmp_path, scenario())
        before = [d.text for d, _ in docs]
        with EvidenceGraph(docs) as graph:
            result = graph.analyze()
            assert len(joins(result)) == 1
            witness = joins(result)[0]
            assert len(witness.evidence_ids) == 3
            evidence_text = [graph.local_evidence(result, token)["sources"][0]["source_text"] for token in witness.evidence_ids]
            assert evidence_text == [("001", "示例企业甲"), ("001", "M01"), ("M01", "secret")]
            assert not any(rule == "numbered_join" for rule, _, _ in graph.per_file_baseline())
            report = public_graph_report(graph, result)
            assert report["incremental_numbered_joins"] == 1
            assert report["privacy_verified"] is False
            for raw in ("示例企业甲", "clients.csv", "M01", "customer", "source_text", "locator"):
                assert raw not in json.dumps(report, ensure_ascii=False)
        assert before == [d.text for d, _ in docs]
        assert [(tmp_path / name).read_bytes().decode("utf-8") for name, _, _ in scenario()] == before


@pytest.mark.parametrize("mutation", ["wrong_key", "leading_zero", "namespace", "type", "negative", "quoted", "unknown", "unlisted_value"])
def test_bridge_negative_controls(tmp_path, mutation):
    entries = scenario()
    if mutation == "wrong_key":
        entries[1] = ("bridge.csv", "cid,mid\n002,M01\n", BRIDGE)
    elif mutation == "leading_zero":
        entries[1] = ("bridge.csv", "cid,mid\n1,M01\n", BRIDGE)
    elif mutation in {"namespace", "type"}:
        key = replace(BRIDGE.fields[0], **({"namespace": "different"} if mutation == "namespace" else {"entity_type": "person"}))
        entries[1] = (entries[1][0], entries[1][1], replace(BRIDGE, fields=(key, BRIDGE.fields[1])))
    elif mutation == "unlisted_value":
        entries[2] = ("event.md", "mid=M01;status=not secret\n", EVENT)
    elif mutation == "quoted":
        entries[2] = ("event.md", ">mid=M01;status=secret\n", EVENT)
    else:
        entries[2] = ("event.md", f"mid=M01;status=secret;assertion={mutation}\n", replace(EVENT, state_field="assertion"))
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        assert not joins(graph.analyze())


def test_duplicate_facts_and_files_do_not_add_strength(tmp_path):
    entries = scenario()
    entries += [("copy.csv", entries[1][1], BRIDGE), ("extra.csv", "cid,mid\n001,M01\n001,M01\n", BRIDGE)]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        result = graph.analyze()
        assert len(result.evidence) == 3
        assert len(joins(result)) == 1
        link = next(e for e in result.evidence if e.kind == "relation")
        assert len(graph.local_evidence(result, link.evidence_id)["sources"]) == 4
        graph.reject_evidence(result, link.evidence_id)
        with pytest.raises(StaleAnalysisError):
            public_graph_report(graph, result)
        assert not joins(graph.analyze())


def test_same_name_only_candidate_shared_org_no_person_merge(tmp_path):
    people = T((F("pid", "key", "people", "person"), F("name", "identity"), F("org", "key", "employer", "organization")), "pid")
    payroll = T((F("pid", "key", "people", "person"), F("status", "sensitive", values=("secret",))), "pid")
    entries = [("people.csv", "pid,name,org\nP1,张三,O1\nP2,张三,O1\n", people),
               ("pay.csv", "pid,status\nP2,secret\n", payroll)]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        result = graph.analyze()
        assert len(result.candidates) == 1 and len(result.candidates[0].entity_ids) == 2
        assert len(joins(result)) == 1
        assert len(set(graph.resolver.assignments.values())) == 3


def test_quasi_support_uses_subjects_not_files_or_rows(tmp_path):
    entries = [("q1.csv", "cid,city,job\nC1,深圳,工程师\nC1,深圳,工程师\nC2,深圳,工程师\n", QID),
               ("q2.csv", "cid,city,job\nC1,深圳,工程师\nC3,上海,教师\n", QID)]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        result = graph.analyze(quasi_fields=("city", "job"), max_support=2)
        q = [w for w in result.witnesses if w.rule == "quasi_combination"]
        assert sorted(w.support for w in q) == [1, 2, 2]
        assert {w.population for w in q} == {3}
        assert len(graph.analyze(quasi_fields=("city", "job"), max_support=1).witnesses) == 1


def test_split_confirm_reject_versioning_and_project_isolation(tmp_path):
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, scenario())) as graph:
        first = graph.analyze()
        customer = joins(first)[0].subject_id
        bridge = next(e for e in first.evidence if e.kind == "relation")
        occurrence = graph.local_evidence(first, bridge.evidence_id)["sources"][0]["occurrences"][0]
        resolver = graph.resolver
        with pytest.raises(StaleAnalysisError):
            resolver.split(customer, [occurrence], workspace_id="other", expected_version=0)
        assert resolver.version == 0
        change = resolver.split(customer, [occurrence], workspace_id=resolver.workspace_id, expected_version=0)
        assert change.action == "split" and change.after_version == 1
        with pytest.raises(StaleAnalysisError):
            graph.local_evidence(first, bridge.evidence_id)
        assert not joins(graph.analyze())
        new_customer = resolver.assignments[occurrence]
        resolver.confirm([customer, new_customer], workspace_id=resolver.workspace_id, expected_version=1)
        restored = graph.analyze()
        assert len(joins(restored)) == 1
        resolver.reject_entity(joins(restored)[0].subject_id, workspace_id=resolver.workspace_id, expected_version=2)
        assert not joins(graph.analyze())


def test_alternate_routes_survive_single_bridge_cut(tmp_path):
    via = T((F("cid", "key", "customer", "client"), F("pid", "key", "project", "project")), "cid")
    tail = T((F("pid", "key", "project", "project"), F("mid", "key", "meeting", "event")), "pid")
    entries = scenario() + [("via.csv", "cid,pid\n001,P1\n", via), ("tail.csv", "pid,mid\nP1,M01\n", tail)]
    with DocumentSession(tmp_path) as session, EvidenceGraph(documents(session, tmp_path, entries)) as graph:
        result = graph.analyze()
        witness = joins(result)[0]
        assert len(witness.evidence_ids) == 3
        assert witness.cuts[1].breaks_witness and not witness.cuts[1].blocks_endpoint_route
        graph.reject_evidence(result, witness.cuts[1].evidence_id)
        second = graph.analyze()
        assert len(joins(second)[0].evidence_ids) == 4
        assert all(c.blocks_endpoint_route for c in joins(second)[0].cuts)


@pytest.mark.parametrize("source,template", [
    ("cid,name\n001,甲\n", replace(CLIENT, primary="cid")),
    ("cid,name\n001,甲\n", CLIENT),
])
def test_closed_documents_invalidate_graph(tmp_path, source, template):
    session = DocumentSession(tmp_path)
    graph = EvidenceGraph(documents(session, tmp_path, [("a.csv", source, template)]))
    result = graph.analyze()
    session.close()
    with pytest.raises(ClosedSessionError):
        public_graph_report(graph, result)
    graph.close()
    assert not graph.resolver.occurrences and not graph._raw


@pytest.mark.parametrize("bad", ["cid,name\n,甲\n", "wrong,name\n001,甲\n", "cid,cid,name\n1,1,甲\n", "cid,name\n001\n"])
def test_bad_records_fail_instead_of_zero_risk(tmp_path, bad):
    with DocumentSession(tmp_path) as session:
        with pytest.raises(ProcessingStopped):
            EvidenceGraph(documents(session, tmp_path, [("a.csv", bad, CLIENT)]))


def test_limits_cancel_and_partial_parse_are_not_success(tmp_path):
    with DocumentSession(tmp_path) as session:
        docs = documents(session, tmp_path, scenario())
        with pytest.raises(ProcessingStopped, match="RECORD_LIMIT"):
            EvidenceGraph(docs, limits=GraphLimits(max_records=1))
        with EvidenceGraph(docs, limits=GraphLimits(max_witnesses=1)) as graph:
            with pytest.raises(ProcessingStopped, match="WITNESS_LIMIT"):
                graph.analyze()
        cancel = threading.Event()
        with EvidenceGraph(docs, cancel=cancel) as graph:
            cancel.set()
            with pytest.raises(ProcessingStopped, match="CANCELLED"):
                graph.analyze()
    with DocumentSession(tmp_path, limits=Limits(max_file_bytes=1)) as session:
        with pytest.raises(ProcessingStopped, match="PARSING_INCOMPLETE"):
            EvidenceGraph(documents(session, tmp_path, scenario()))


def config_for_scenario():
    return {"schema_version": "d2-templates-1", "files": [
        {"path": name, "primary": template.primary, "fields": [
            {"name": f.name, "role": f.role, "namespace": f.namespace, "entity_type": f.entity_type.value, "values": list(f.values)} for f in template.fields]}
        for name, _, template in scenario()]}


def test_cli_real_entry_no_model_database_or_plaintext(tmp_path):
    for name, text, _ in scenario():
        (tmp_path / name).write_text(text, encoding="utf-8")
    config = tmp_path / "relations.json"
    config.write_text(json.dumps(config_for_scenario()), encoding="utf-8")
    before = set(tmp_path.iterdir())
    result = subprocess.run([sys.executable, "-m", "privacyfs.cli", "relate", str(tmp_path), "--config", str(config)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["incremental_numbered_joins"] == 1
    assert set(tmp_path.iterdir()) == before
    assert "示例企业甲" not in result.stdout and "Traceback" not in result.stderr
    (tmp_path / "bridge.csv").write_text("bad", encoding="utf-8")
    failed = CliRunner().invoke(app, ["relate", str(tmp_path), "--config", str(config)])
    assert failed.exit_code == 4
    assert json.loads(failed.stdout)["risk_count"] is None


@pytest.mark.parametrize("change", ["unknown", "duplicate", "bad_sensitive", "wrong_schema"])
def test_strict_config_errors(tmp_path, change):
    config = config_for_scenario()
    if change == "unknown": config["extra"] = True
    if change == "bad_sensitive": config["files"][2]["fields"][1]["values"] = []
    if change == "wrong_schema": config["schema_version"] = "new"
    payload = json.dumps(config)
    if change == "duplicate": payload = payload.replace('"schema_version":', '"schema_version":"x","schema_version":', 1)
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError): load_relation_config(path)


def test_order_invariance_conflicting_qids_and_csv_location(tmp_path):
    entries = scenario() + [("q.csv", 'cid,city,job\n001,"深,圳",工程师\n001,上海,工程师\n', QID)]
    with DocumentSession(tmp_path) as session:
        docs = documents(session, tmp_path, entries)
        summaries = []
        for ordered in (docs, list(reversed(docs))):
            with EvidenceGraph(ordered) as graph:
                result = graph.analyze(quasi_fields=("city", "job"))
                summaries.append(sorted(w.rule for w in result.witnesses))
                assert "quasi_combination" not in summaries[-1]
                q = next(e for e in result.evidence if e.value == "深,圳")
                assert graph.local_evidence(result, q.evidence_id)["sources"][0]["source_text"][1] == '深,圳'
        assert summaries[0] == summaries[1]


def test_mixed_sessions_and_changed_document_revisions_rejected(tmp_path):
    with DocumentSession(tmp_path) as one, DocumentSession(tmp_path) as two:
        a = documents(one, tmp_path, scenario())
        b = documents(two, tmp_path, scenario())
        with pytest.raises(ValueError): EvidenceGraph([a[0], b[1]])
        new_doc = one.parse(one.capture("clients.csv"))
        with pytest.raises(ValueError): EvidenceGraph([a[0], (new_doc, CLIENT)])
