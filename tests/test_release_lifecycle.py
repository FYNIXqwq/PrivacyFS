"""Real private state, approval binding and staged publication with synthetic data."""
import json
from pathlib import Path

import pytest

from privacyfs.release import (prepare_release, review_release, export_release, recover_release,
                               show_release, ReleaseStore, verify_tree)
from privacyfs.verifier import ReleaseRejected
from test_cross_file_risk import scenario, config_for_scenario


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name, text, _ in scenario():
        (root / name).write_bytes(text.encode("utf-8"))
    relations = root / "relations.json"
    relations.write_text(json.dumps(config_for_scenario()), encoding="utf-8")
    profile = root / "profile.json"
    profile.write_text(json.dumps({"schema_version": "d3-task-profile-1", "task": "collaboration_statistics",
        "recipient": "synthetic-local-recipient", "fields": [
            {"name": "cid", "output_name": "customer_id", "mode": "alias"},
            {"name": "mid", "output_name": "meeting_id", "mode": "alias"},
            {"name": "status", "output_name": "status", "approved_values": ["secret", "ended"]}]}), encoding="utf-8")
    return root, relations, profile, tmp_path / "private", tmp_path / "outputs"


def prepare(project):
    return prepare_release(*project[:4])


def approve(result, project):
    return review_release(result["plan_id"], project[3], approve=result["approval_digest"])


def test_prepare_approve_publish_and_old_source_bytes_preserved(project):
    root, _, _, private, outputs = project
    original = {p.name: p.read_bytes() for p in root.iterdir()}
    result = prepare(project)
    assert result["state"] == "VALIDATED"
    assert not outputs.exists()
    with pytest.raises(ReleaseRejected, match="EXPLICIT_APPROVAL"):
        export_release(result["plan_id"], outputs, private)
    approve(result, project)
    published = export_release(result["plan_id"], outputs, private)
    assert published["state"] == "PUBLISHED"
    destination = outputs / result["release_id"]
    assert destination.is_dir()
    assert len(list(destination.iterdir())) == 4
    assert {p.name: p.read_bytes() for p in root.iterdir()} == original
    assert "示例企业甲".encode() not in b"".join(p.read_bytes() for p in destination.iterdir())
    assert show_release(result["plan_id"], private)["artifact_integrity"] == "verified_against_publication"
    with pytest.raises(ReleaseRejected):
        export_release(result["plan_id"], outputs, private)


@pytest.mark.parametrize("change", ["source", "recipient", "profile", "rules"])
def test_approved_plan_cannot_publish_changed_inputs(project, change):
    result = prepare(project)
    approve(result, project)
    root, relations, profile, private, outputs = project
    if change == "source":
        source = root / "event.md"
        source.write_text("mid=M01;status=ended\n", encoding="utf-8")
    elif change in {"profile", "recipient"}:
        data = json.loads(profile.read_text())
        if change == "recipient": data["recipient"] = "another"
        else: data["preserve_rows"] = False
        profile.write_text(json.dumps(data), encoding="utf-8")
    else:
        data = json.loads(relations.read_text())
        data["files"][0]["default_state"] = "negative"
        relations.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ReleaseRejected):
        export_release(result["plan_id"], outputs, private)
    assert not (outputs / result["release_id"]).exists()
    assert review_release(result["plan_id"], private)["state"] == "NEEDS_REVIEW"


def test_wrong_approval_digest_and_state_tampering_refused(project):
    result = prepare(project)
    with pytest.raises(ReleaseRejected, match="APPROVAL_DIGEST"):
        review_release(result["plan_id"], project[3], approve="0" * 64)
    file = project[3] / "plans" / (result["plan_id"] + ".json")
    data = json.loads(file.read_text(encoding="utf-8"))
    data["payload"]["state"] = "APPROVED"
    file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ReleaseRejected, match="STATE_INTEGRITY"):
        review_release(result["plan_id"], project[3])


@pytest.mark.parametrize("boundary", ["source", "state", "ancestor"])
def test_output_boundaries_and_existing_release_protected(project, boundary):
    result = prepare(project)
    approve(result, project)
    destination = {"source": project[0] / "out", "state": project[3] / "out", "ancestor": project[0].parent}[boundary]
    with pytest.raises(ReleaseRejected, match="OVERLAP"):
        export_release(result["plan_id"], destination, project[3])


def test_write_failure_preserves_previous_release(project, monkeypatch):
    import privacyfs.release as release
    first = prepare(project)
    approve(first, project)
    export_release(first["plan_id"], project[4], project[3])
    previous = {p.name: p.read_bytes() for p in (project[4] / first["release_id"]).iterdir()}
    second = prepare(project)
    approve(second, project)
    original = release.write_new
    def fail(path, payload):
        if path.name.startswith("DOC_"):
            raise OSError("synthetic failure")
        return original(path, payload)
    monkeypatch.setattr(release, "write_new", fail)
    with pytest.raises(OSError):
        export_release(second["plan_id"], project[4], project[3])
    assert {p.name: p.read_bytes() for p in (project[4] / first["release_id"]).iterdir()} == previous
    assert not (project[4] / second["release_id"]).exists()
    assert review_release(second["plan_id"], project[3])["state"] == "FAILED"


@pytest.mark.parametrize("after_rename", [False, True])
def test_interrupted_switch_is_reconciled_without_overwriting(project, monkeypatch, after_rename):
    import privacyfs.release as release
    result = prepare(project)
    approve(result, project)
    original = release.publish_rename
    def interrupt(stage, target):
        if after_rename:
            original(stage, target)
        raise SystemExit("synthetic process interruption")
    monkeypatch.setattr(release, "publish_rename", interrupt)
    with pytest.raises(SystemExit):
        export_release(result["plan_id"], project[4], project[3])
    assert review_release(result["plan_id"], project[3])["state"] == "STAGING"
    recovered = recover_release(result["plan_id"], project[3])
    assert recovered["state"] == ("PUBLISHED" if after_rename else "APPROVED")
    assert not (project[3] / "staging" / result["plan_id"]).exists()


@pytest.mark.parametrize("tamper", ["extra", "bytes"])
def test_published_extra_file_and_byte_changes_detected(project, tamper):
    result = prepare(project)
    approve(result, project)
    export_release(result["plan_id"], project[4], project[3])
    destination = project[4] / result["release_id"]
    if tamper == "extra":
        (destination / "extra.txt").write_text("synthetic", encoding="utf-8")
    else:
        next(destination.glob("DOC_*")).write_bytes(b"changed")
    with pytest.raises(ReleaseRejected):
        show_release(result["plan_id"], project[3])


def test_real_process_exit_after_rename_is_recoverable(project):
    import subprocess
    import sys
    result = prepare(project)
    approve(result, project)
    script = """import os,sys
import privacyfs.release as r
original = r.publish_rename
def crash(stage,target):
    original(stage,target)
    os._exit(71)
r.publish_rename=crash
r.export_release(sys.argv[1],sys.argv[2],sys.argv[3])
"""
    exited = subprocess.run([sys.executable, "-c", script, result["plan_id"], str(project[4]), str(project[3])], capture_output=True)
    assert exited.returncode == 71
    assert recover_release(result["plan_id"], project[3])["state"] == "PUBLISHED"
    assert show_release(result["release_id"], project[3])["artifact_integrity"] == "verified_against_publication"


def test_existing_destination_is_not_overwritten(project):
    result = prepare(project)
    approve(result, project)
    existing = project[4] / result["release_id"]
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_bytes(b"keep")
    with pytest.raises(ReleaseRejected, match="DESTINATION_EXISTS"):
        export_release(result["plan_id"], project[4], project[3])
    assert (existing / "keep.txt").read_bytes() == b"keep"


def test_broken_input_revokes_approval_and_private_store_lock_excludes_second_writer(project):
    result = prepare(project)
    approve(result, project)
    (project[0] / "clients.csv").write_text("invalid", encoding="utf-8")
    with pytest.raises(Exception):
        export_release(result["plan_id"], project[4], project[3])
    assert review_release(result["plan_id"], project[3])["state"] == "NEEDS_REVIEW"
    store = ReleaseStore(project[3])
    with store.lock():
        with pytest.raises(OSError):
            with ReleaseStore(project[3]).lock():
                pytest.fail("second writer acquired lock")


def test_state_inside_source_and_foreign_directory_refused(project):
    with pytest.raises(ReleaseRejected, match="STATE_SOURCE_OVERLAP"):
        prepare_release(*project[:3], project[0] / "private")
    project[3].mkdir()
    (project[3] / "foreign.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        prepare(project)
    assert (project[3] / "foreign.txt").read_text() == "preserve"
