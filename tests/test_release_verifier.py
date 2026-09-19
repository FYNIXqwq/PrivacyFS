"""Independent task outputs, ambiguity, transformations and residual attacks."""
from dataclasses import replace
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.documents import DocumentSession
from privacyfs.entities import StaleAnalysisError
from privacyfs.evidence import EvidenceGraph
from privacyfs.planning import build_plan
from privacyfs.relations import FieldSpec as F, RecordTemplate as T
from privacyfs.task_profiles import FieldPolicy as P, TaskProfile, load_task_profile
from privacyfs.verifier import ReleaseRejected, verify_candidate
from test_cross_file_risk import CLIENT, EVENT, QID, documents, scenario
from test_release_planning import profile
from test_release_lifecycle import project, prepare, approve


@pytest.mark.parametrize("task", ["collaboration_statistics", "record_summary", "incident_diagnostics"])
def test_three_task_profiles_preserve_exact_fact_values_and_status_counts(tmp_path, task):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), replace(profile(), task=task))
            result = plan.candidates[plan.selected]
            rows = result.artifacts[2].payload.decode().splitlines()
            assert len(rows) == 1 and "status=secret;assertion_state=positive" in rows[0]
            assert result.loss < plan.candidates[-1].loss


def test_quasi_risk_forces_optional_field_drop_instead_of_fake_safety(tmp_path):
    template = T((F("cid", "key", "customer"), F("fact", "sensitive", values=("ok",)),
                  F("city", "quasi"), F("job", "quasi")), "cid")
    task = TaskProfile("record_summary", "local", (
        P("fact", "fact", approved_values=("ok",)),
        P("city", "city", required=False, approved_values=("深圳",)),
        P("job", "job", required=False, approved_values=("工程师",))))
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, [("q.csv", "cid,fact,city,job\nC1,ok,深圳,工程师\n", template)])
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), task)
            assert plan.candidates[0].reason == "OUTPUT_RISK_REMAINS"
            assert plan.selected == 1
            assert "深圳".encode() not in plan.candidates[1].artifacts[0].payload


def test_incomplete_union_of_quasi_fields_cannot_hide_a_smaller_risky_subset(tmp_path):
    first = T((F("id", "key", "people", "person"), F("fact", "sensitive", values=("ok",)), F("city", "quasi"), F("job", "quasi")), "id")
    second = T((F("id", "key", "companies", "organization"), F("fact", "sensitive", values=("ok",)), F("industry", "quasi"), F("size", "quasi")), "id")
    task = TaskProfile("record_summary", "local", (P("fact", "fact", approved_values=("ok",)),
        *[P(name, name, required=False, approved_values=(value,)) for name, value in (("city", "SZ"), ("job", "dev"), ("industry", "IT"), ("size", "small"))]))
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, [("a.csv", "id,fact,city,job\nP1,ok,SZ,dev\n", first),
                                                ("b.csv", "id,fact,industry,size\nO1,ok,IT,small\n", second)])
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), task)
            assert plan.candidates[0].reason == "OUTPUT_RISK_REMAINS"
            assert plan.selected == 1


def test_explicit_generalization_and_csv_quotes_multiline_values(tmp_path):
    template = T((F("cid", "key", "customer"), F("fact", "sensitive", values=('line1\nline2,"quoted"',)), F("city", "quasi")), "cid")
    task = TaskProfile("record_summary", "local", (
        P("fact", "fact", approved_values=('line1\nline2,"quoted"',)),
        P("city", "region", "generalize", generalizations=(("深圳", "华南"),))))
    text = 'cid,fact,city\nC1,"line1\nline2,""quoted""",深圳\n'
    with DocumentSession(tmp_path) as session:
        (tmp_path / "a.csv").write_bytes(text.encode("utf-8"))
        bindings = [(session.parse(session.capture("a.csv")), template)]
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), task)
            candidate = plan.candidates[plan.selected]
            actual = list(csv.reader(io.StringIO(candidate.artifacts[0].payload.decode(), newline="")))
            assert actual[1][1:3] == ['line1\nline2,"quoted"', "华南"]
            assert any(a.kind == "generalize" for a in candidate.actions)


def test_unconfigured_sensitive_column_and_original_headers_are_not_exported(tmp_path):
    entries = scenario()
    entries[0] = ("clients.csv", "cid,name,PrivateNotes\n001,示例企业甲,不可释放原文\n", CLIENT)
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, entries)
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), profile())
            candidate = plan.candidates[plan.selected]
            output = b"".join(a.payload for a in candidate.artifacts)
            assert "不可释放原文".encode() not in output and b"PrivateNotes" not in output
            assert any(a.field_index is None and a.kind == "drop" for a in candidate.actions)


def test_drop_graph_evidence_is_not_permission_to_keep_raw_identity(tmp_path):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            result = graph.analyze()
            identity = next(e for e in result.evidence if e.kind == "identity")
            graph.reject_evidence(result, identity.evidence_id)
            plan = build_plan(bindings, graph, graph.analyze(), profile())
            candidate = plan.candidates[plan.selected]
            assert "示例企业甲".encode() not in b"".join(a.payload for a in candidate.artifacts)
            entity = next(iter(graph.resolver.assignments.values()))
            graph.resolver.confirm([entity], workspace_id=graph.resolver.workspace_id, expected_version=graph.resolver.version)
            with pytest.raises(StaleAnalysisError):
                verify_candidate(plan, candidate)


@pytest.mark.parametrize("forbidden", ["secret", "ＳＥＣＲＥＴ", "customer_id"])
def test_forbidden_normalized_value_or_header_makes_plan_infeasible(tmp_path, forbidden):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), replace(profile(), forbidden_terms=(forbidden,)))
            assert plan.selected is None


@pytest.mark.parametrize("attack", ["=1+2", "+SUM(A1)", "-1", "@command"])
def test_spreadsheet_active_values_refused_even_if_allowlisted(tmp_path, attack):
    template = T((F("cid", "key", "customer"), F("fact", "sensitive", values=(attack,))), "cid")
    task = TaskProfile("incident_diagnostics", "local", (P("fact", "fact", approved_values=(attack,)),))
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, [("a.csv", f"cid,fact\nC1,{attack}\n", template)])
        with EvidenceGraph(bindings) as graph:
            assert build_plan(bindings, graph, graph.analyze(), task).selected is None


def test_raw_key_keep_and_unapproved_fact_refused(tmp_path):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            analysis = graph.analyze()
            unsafe = replace(profile(), fields=(P("cid", "cid", approved_values=("001",)), profile().fields[-1]))
            with pytest.raises(ValueError, match="raw values"):
                build_plan(bindings, graph, analysis, unsafe)
            unknown = replace(profile(), fields=(*profile().fields[:2], P("status", "status", approved_values=("other",))))
            with pytest.raises(ReleaseRejected, match="VALUE_NOT_APPROVED"):
                build_plan(bindings, graph, analysis, unknown)


@pytest.mark.parametrize("identity,forbidden,fact", [("Alpha", (), "prefixAlphaSuffix"), ("Beta", ("abc",), "xxabcxx")])
def test_identity_and_explicit_forbidden_substrings_cannot_hide_inside_allowed_value(tmp_path, identity, forbidden, fact):
    template = T((F("cid", "key", "customers"), F("name", "identity"), F("fact", "sensitive", values=(fact,))), "cid")
    task = TaskProfile("record_summary", "local", (P("fact", "fact", approved_values=(fact,)),), forbidden_terms=forbidden)
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, [("a.csv", f"cid,name,fact\nC1,{identity},{fact}\n", template)])
        with EvidenceGraph(bindings) as graph:
            assert build_plan(bindings, graph, graph.analyze(), task).selected is None


@pytest.mark.parametrize("invalid", ["unknown_setting", "duplicate_key", "unknown_task", "wrong_bool", "duplicate_output"])
def test_strict_profile_rejects_ambiguous_constraints(project, invalid):
    path = project[2]
    data = json.loads(path.read_text())
    if invalid == "unknown_setting": data["allow_all"] = True
    elif invalid == "unknown_task": data["task"] = "whatever agent wants"
    elif invalid == "wrong_bool": data["preserve_rows"] = "false"
    elif invalid == "duplicate_output": data["fields"][1]["output_name"] = data["fields"][0]["output_name"]
    text = json.dumps(data)
    if invalid == "duplicate_key": text = text.replace('"recipient":', '"recipient":"x","recipient":')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError): load_task_profile(path)


def test_real_cli_prepare_review_export_show(project):
    command = [sys.executable, "-m", "privacyfs.cli"]
    created = subprocess.run(command + ["prepare", str(project[0]), "--relations", str(project[1]),
        "--task-profile", str(project[2]), "--state-dir", str(project[3])], capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    report = json.loads(created.stdout)
    runner = CliRunner()
    rejected = runner.invoke(app, ["export", report["plan_id"], str(project[4]), "--state-dir", str(project[3])])
    assert rejected.exit_code == 3 and json.loads(rejected.stdout)["reason"] == "EXPLICIT_APPROVAL_REQUIRED"
    approved = runner.invoke(app, ["review", report["plan_id"], "--state-dir", str(project[3]), "--approve", report["approval_digest"]])
    assert approved.exit_code == 0
    published = runner.invoke(app, ["export", report["plan_id"], str(project[4]), "--state-dir", str(project[3])])
    assert published.exit_code == 0 and json.loads(published.stdout)["state"] == "PUBLISHED"
    shown = runner.invoke(app, ["release", "show", report["release_id"], "--state-dir", str(project[3])])
    assert shown.exit_code == 0
    assert str(project[0]) not in shown.stdout and "synthetic-local-recipient" not in shown.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS streams")
def test_actual_ntfs_alternate_stream_is_rejected_before_publication(project, monkeypatch):
    import privacyfs.release as release
    result = prepare(project)
    approve(result, project)
    original = release.write_new
    def inject(path, payload):
        original(path, payload)
        if path.name.startswith("DOC_"):
            Path(str(path) + ":hidden").write_bytes(b"synthetic hidden content")
    monkeypatch.setattr(release, "write_new", inject)
    with pytest.raises(ReleaseRejected, match="ALTERNATE_STREAM"):
        release.export_release(result["plan_id"], project[4], project[3])
    assert not (project[4] / result["release_id"]).exists()


def test_source_change_during_staging_invalidates_approval(project, monkeypatch):
    import privacyfs.release as release
    result = prepare(project)
    approve(result, project)
    original = release.write_new
    def inject(path, payload):
        original(path, payload)
        if path.name.startswith("DOC_"):
            (project[0] / "event.md").write_bytes(b"mid=M01;status=ended\n")
    monkeypatch.setattr(release, "write_new", inject)
    with pytest.raises(ReleaseRejected):
        release.export_release(result["plan_id"], project[4], project[3])
    assert release.review_release(result["plan_id"], project[3])["state"] == "NEEDS_REVIEW"
    assert not (project[4] / result["release_id"]).exists()
