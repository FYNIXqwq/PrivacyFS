"""Durable corrections, revision binding and local/private review boundaries."""
import json

import pytest

from privacyfs.review import (refresh_workspace, correct_workspace, undo_workspace,
    local_workspace_details, prepare_workspace, configure_workspace, local_plan_details,
    run_local_console, terminal_text, forget_workspace, retention_workspace)
from privacyfs.release import ReleaseStore, review_release, export_release, show_release
from privacyfs.verifier import ReleaseRejected
from test_release_lifecycle import project
from test_incremental import start, semantic, refresh


def test_durable_split_undo_and_release_approval_invalidation(project):
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    prepared = prepare_workspace(workspace, project[3])
    review_release(prepared["plan_id"], project[3], approve=prepared["approval_digest"])
    local = local_workspace_details(workspace, project[3], expected_revision=report["revision"])
    occurrence = next(o for o in local["occurrences"] if o["path"].endswith("bridge.csv") and o["value"] == "001")
    updated = correct_workspace(workspace, project[3], "split", [occurrence["entity_id"], occurrence["occurrence_id"]], expected_revision=report["revision"])
    assert not any(w["rule"] == "numbered_join" for w in updated["risks"])
    assert review_release(prepared["plan_id"], project[3])["state"] == "NEEDS_REVIEW"
    assert semantic(updated) == semantic(refresh(project, workspace, force=True))
    after = prepare_workspace(workspace, project[3])
    before_after = local_plan_details(after["plan_id"], project[3])
    assert any("示例企业甲" in d["before"] for d in before_after)
    assert all("示例企业甲" not in d["after"] for d in before_after)
    review_release(after["plan_id"], project[3], approve=after["approval_digest"])
    assert export_release(after["plan_id"], project[4], project[3])["state"] == "PUBLISHED"
    undone = undo_workspace(workspace, project[3], expected_revision=updated["revision"])
    assert any(w["rule"] == "numbered_join" for w in undone["risks"])
    assert show_release(after["release_id"], project[3])["state"] == "PUBLISHED"


def test_reject_evidence_persists_and_stale_source_requires_review(project):
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    local = local_workspace_details(workspace, project[3], expected_revision=report["revision"])
    eid = next(e["evidence_id"] for e in local["evidence"] if e["kind"] == "relation")
    rejected = correct_workspace(workspace, project[3], "reject_evidence", [eid], expected_revision=report["revision"])
    assert rejected["risk_count"] == 1
    assert semantic(rejected) == semantic(refresh(project, workspace, force=True))
    (project[0] / "bridge.csv").write_bytes(b"cid,mid\n001,M02\n")
    changed = refresh_workspace(workspace, project[3])
    assert changed["status"] == "needs_review"
    with pytest.raises(ReleaseRejected, match="STALE_CORRECTIONS"):
        prepare_workspace(workspace, project[3])
    latest = refresh_workspace(workspace, project[3])
    clear = undo_workspace(workspace, project[3], expected_revision=latest["revision"])
    assert clear["status"] == "ready"


def test_confirm_same_name_candidates_without_cross_project_pollution(project):
    root, relations, profile, state, _ = project
    (root / "clients.csv").write_bytes("cid,name\n001,同名主体\n002,同名主体\n".encode())
    workspace = start(project)
    first = refresh_workspace(workspace, state)
    candidate = first["candidates"][0]
    rejected = correct_workspace(workspace, state, "reject_candidate", [candidate["candidate_id"]], expected_revision=first["revision"])
    assert rejected["candidates"][0]["state"] == "rejected"
    before = undo_workspace(workspace, state, expected_revision=rejected["revision"])
    confirmed = correct_workspace(workspace, state, "confirm", list(candidate["entity_ids"]), expected_revision=before["revision"])
    assert not confirmed["candidates"]
    assert semantic(confirmed) == semantic(refresh(project, workspace, force=True))
    with pytest.raises(ReleaseRejected, match="REVISION"):
        correct_workspace(workspace, state, "reject_entity", [confirmed["entities"][0]["entity_id"]], expected_revision=first["revision"])


def test_template_path_change_and_undo_are_versioned(project):
    workspace = start(project)
    first = refresh_workspace(workspace, project[3])
    replacement = project[0] / "second-profile.json"
    data = json.loads(project[2].read_text())
    data["recipient"] = "another-local-recipient"
    replacement.write_text(json.dumps(data), encoding="utf-8")
    configured = configure_workspace(workspace, project[3], "profile", replacement, expected_revision=first["revision"])
    assert configured["revision"] > first["revision"]
    assert configured["metrics"]["graph_cache_hits"] == 1
    prepared = prepare_workspace(workspace, project[3])
    undo_workspace(workspace, project[3], expected_revision=configured["revision"])
    assert review_release(prepared["plan_id"], project[3])["state"] == "NEEDS_REVIEW"


def test_local_console_and_safe_headless_outputs(project):
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    assert "示例企业甲" not in json.dumps(report, ensure_ascii=False)
    assert str(project[0]) not in json.dumps(report)
    commands = iter(["sources", "occurrences", "plan", "quit"])
    printed = []
    run_local_console(workspace, project[3], read=lambda _: next(commands), write=printed.append)
    transcript = "\n".join(printed)
    assert "clients.csv" in transcript and "示例企业甲" in transcript
    assert "before" in transcript and "after" in transcript
    assert "\x1b" not in terminal_text("\x1b[2J")
    assert "\u202e" not in terminal_text("\u202e")


def test_retention_and_forget_preserve_published_history(project):
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    prepared = prepare_workspace(workspace, project[3])
    review_release(prepared["plan_id"], project[3], approve=prepared["approval_digest"])
    export_release(prepared["plan_id"], project[4], project[3])
    retention_workspace(workspace, project[3], keep_cache=True, expected_revision=report["revision"])
    (project[0] / "event.md").write_bytes(b"mid=M01;status=ended\n")
    updated = refresh_workspace(workspace, project[3])
    report = retention_workspace(workspace, project[3])
    assert report["cache_files"] > 0 and report["dry_run"]
    deleted = retention_workspace(workspace, project[3], apply=True, expected_revision=updated["revision"])
    assert deleted["cache_files"] == report["cache_files"]
    result = forget_workspace(workspace, project[3], expected_revision=updated["revision"])
    assert result["status"] == "forgotten"
    assert not (project[3] / "workspaces" / workspace).exists()
    assert show_release(prepared["release_id"], project[3])["state"] == "PUBLISHED"
