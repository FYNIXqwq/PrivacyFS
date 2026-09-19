"""D3 task fidelity and actual content verification, all synthetic."""
from dataclasses import replace
import csv
import io

import pytest

from privacyfs.documents import DocumentSession
from privacyfs.evidence import EvidenceGraph
from privacyfs.task_profiles import FieldPolicy as P, TaskProfile
from privacyfs.planning import build_plan
from privacyfs.verifier import verify_candidate, ReleaseRejected
from test_cross_file_risk import scenario, documents


def profile():
    return TaskProfile("collaboration_statistics", "synthetic-recipient", (
        P("cid", "customer_id", "alias"), P("mid", "meeting_id", "alias"),
        P("status", "status", approved_values=("secret", "ended"))))


def test_projected_release_preserves_task_and_links_without_identity(tmp_path):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), profile())
            assert plan.selected is not None
            candidate = plan.candidates[plan.selected]
            verify_candidate(plan, candidate)
            assert candidate.feasible and not plan.candidates[-1].feasible
            output = b"".join(a.payload for a in candidate.artifacts).decode("utf-8")
            assert "示例企业甲" not in output and "M01" not in output and "001" not in output.split()
            assert "secret" in output
            assert len({row[0] for a in candidate.artifacts[:2] for row in a.rows}) == 1
            assert any(a.kind == "drop" for a in candidate.actions)
            assert any(a.kind == "alias" for a in candidate.actions)


def test_independent_actual_bytes_verification_rejects_injection(tmp_path):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), profile())
            candidate = plan.candidates[plan.selected]
            artifacts = list(candidate.artifacts)
            artifacts[0] = replace(artifacts[0], payload=artifacts[0].payload + "\n示例企业甲".encode())
            with pytest.raises(ReleaseRejected):
                verify_candidate(plan, replace(candidate, artifacts=tuple(artifacts)))


def test_scope_aliases_isolate_releases(tmp_path):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            analysis = graph.analyze()
            a = build_plan(bindings, graph, analysis, profile())
            b = build_plan(bindings, graph, analysis, profile())
            assert a.candidates[0].artifacts[0].rows != b.candidates[0].artifacts[0].rows


def test_raw_keys_and_required_identity_constraints_refused():
    with pytest.raises(ValueError):
        P("name", "name", "drop", required=True)


def test_new_snapshot_or_changed_template_cannot_reuse_old_entity_graph(tmp_path):
    with DocumentSession(tmp_path) as session:
        bindings = documents(session, tmp_path, scenario())
        with EvidenceGraph(bindings) as graph:
            analysis = graph.analyze()
            (tmp_path / "clients.csv").write_bytes("cid,name\n002,另一个主体\n".encode())
            newer = session.parse(session.capture("clients.csv"))
            with pytest.raises(ValueError, match="snapshot or template"):
                build_plan([(newer, bindings[0][1]), *bindings[1:]], graph, analysis, profile())
            altered = replace(bindings[0][1], default_state="negative")
            with pytest.raises(ValueError, match="snapshot or template"):
                build_plan([(bindings[0][0], altered), *bindings[1:]], graph, analysis, profile())
