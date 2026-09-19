"""State encryption/migration, change classes, crash and cancellation boundaries."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.release import (ReleaseStore, canonical, STORE_MARKER, review_release, export_release)
from privacyfs.state_store import WorkspaceStore, protect_existing_key
from privacyfs.review import (refresh_workspace, prepare_workspace, correct_workspace, workspace_status,
                              local_workspace_details)
from privacyfs.documents.models import ProcessingStopped
from privacyfs.verifier import ReleaseRejected
from test_release_lifecycle import project, prepare
from test_incremental import start, semantic, refresh


def test_duplicates_renames_and_deletions_do_not_inflate_subjects(project):
    workspace = start(project)
    first = refresh_workspace(workspace, project[3])
    settings = json.loads(project[1].read_text())
    copy = dict(settings["files"][1])
    copy["path"] = "copy.csv"
    settings["files"].append(copy)
    (project[0] / "copy.csv").write_bytes((project[0] / "bridge.csv").read_bytes())
    project[1].write_text(json.dumps(settings), encoding="utf-8")
    duplicate = refresh_workspace(workspace, project[3])
    assert duplicate["risk_count"] == first["risk_count"]
    assert duplicate["entities"] == first["entities"]
    assert duplicate["metrics"].get("parsed_documents", 0) == 0
    (project[0] / "copy.csv").rename(project[0] / "renamed.csv")
    settings["files"][-1]["path"] = "renamed.csv"
    project[1].write_text(json.dumps(settings), encoding="utf-8")
    renamed = refresh_workspace(workspace, project[3])
    assert any("possible_rename_to" in e for e in renamed["changes"])
    assert renamed["metrics"].get("parsed_documents", 0) == 0
    settings["files"].pop()
    (project[0] / "renamed.csv").unlink()
    project[1].write_text(json.dumps(settings), encoding="utf-8")
    deleted = refresh_workspace(workspace, project[3])
    assert deleted["entities"] == first["entities"]
    assert semantic(deleted) == semantic(refresh(project, workspace, force=True))


def test_parse_version_change_marks_corrections_stale(project, monkeypatch):
    import privacyfs.incremental as incremental
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    local = local_workspace_details(workspace, project[3], expected_revision=report["revision"])
    eid = next(e["evidence_id"] for e in local["evidence"] if e["kind"] == "relation")
    correct_workspace(workspace, project[3], "reject_evidence", [eid], expected_revision=report["revision"])
    monkeypatch.setattr(incremental, "CACHE_VERSION", "test-new-parser")
    updated = refresh_workspace(workspace, project[3])
    assert updated["status"] == "needs_review"
    assert updated["metrics"]["parsed_documents"] == 3
    with pytest.raises(ReleaseRejected, match="STALE_CORRECTIONS"):
        prepare_workspace(workspace, project[3])


def test_running_cancel_leaves_last_committed_workspace_unchanged(project, monkeypatch):
    import privacyfs.incremental as incremental
    workspace = start(project)
    before = refresh_workspace(workspace, project[3])
    cancel = threading.Event()
    original = incremental.dump_document
    def cancel_after_parse(doc):
        result = original(doc)
        cancel.set()
        return result
    monkeypatch.setattr(incremental, "dump_document", cancel_after_parse)
    with pytest.raises(ProcessingStopped, match="CANCELLED"):
        refresh_workspace(workspace, project[3], force=True, cancel=cancel)
    assert workspace_status(workspace, project[3])["revision"] == before["revision"]


def test_cancel_during_export_never_publishes_partial_files(project, monkeypatch):
    import privacyfs.release as release
    workspace = start(project)
    prepared = prepare_workspace(workspace, project[3])
    review_release(prepared["plan_id"], project[3], approve=prepared["approval_digest"])
    cancel = threading.Event()
    original = release.write_new
    def cancel_after_first_file(path, payload):
        original(path, payload)
        if path.name.startswith("DOC_"):
            cancel.set()
    monkeypatch.setattr(release, "write_new", cancel_after_first_file)
    with pytest.raises(ProcessingStopped, match="CANCELLED"):
        export_release(prepared["plan_id"], project[4], project[3], cancel=cancel)
    assert review_release(prepared["plan_id"], project[3])["state"] == "CANCELLED"
    assert not (project[4] / prepared["release_id"]).exists()


def test_committed_correction_still_invalidates_plan_if_bookkeeping_is_interrupted(project, monkeypatch):
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    prepared = prepare_workspace(workspace, project[3])
    review_release(prepared["plan_id"], project[3], approve=prepared["approval_digest"])
    local = local_workspace_details(workspace, project[3], expected_revision=report["revision"])
    eid = next(e["evidence_id"] for e in local["evidence"] if e["kind"] == "relation")
    def interrupted(*args):
        raise SystemExit("synthetic commit interruption")
    monkeypatch.setattr(WorkspaceStore, "invalidate_plans", interrupted)
    with pytest.raises(SystemExit):
        correct_workspace(workspace, project[3], "reject_evidence", [eid], expected_revision=report["revision"])
    assert review_release(prepared["plan_id"], project[3])["state"] == "NEEDS_REVIEW"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI encryption")
def test_private_state_contains_no_plaintext_originals_or_plaintext_keys(project):
    workspace = start(project)
    refresh_workspace(workspace, project[3])
    prepared = prepare_workspace(workspace, project[3])
    marker = json.loads((project[3] / STORE_MARKER).read_text())
    assert marker["provider"] == "windows-dpapi-user" and "key" not in marker
    raw = b"".join(p.read_bytes() for p in project[3].rglob("*.json"))
    assert "示例企业甲".encode() not in raw
    assert str(project[0]).encode() not in raw
    record = ReleaseStore(project[3]).load(prepared["plan_id"])
    assert record["seed"].encode() not in raw


def test_d3_plaintext_key_and_records_migrate_without_reissuing_approval(project):
    result = prepare(project)
    store = ReleaseStore(project[3])
    record = store.load(result["plan_id"])
    # Synthetic old-format storage, with exactly the same key and plan binding.
    (project[3] / STORE_MARKER).write_bytes(canonical({"schema_version": "d3-state-1", "key": store.key.hex()}))
    (project[3] / "plans" / (result["plan_id"] + ".json")).write_bytes(canonical({"payload": record, "mac": store.mac(canonical(record))}))
    loaded = ReleaseStore(project[3])
    with loaded.lock():
        protect_existing_key(loaded)
    migrated = ReleaseStore(project[3])
    assert migrated.key == store.key
    assert migrated.load(result["plan_id"]) == record
    assert "private" in json.loads((project[3] / "plans" / (result["plan_id"] + ".json")).read_text())


def test_corrupt_cache_refuses_results_and_full_refresh_repairs_it(project):
    workspace = start(project)
    refresh_workspace(workspace, project[3])
    store = WorkspaceStore(ReleaseStore(project[3]))
    record = store.load(workspace)
    cache = store.cache_path(workspace, next(iter(record["components"].values())))
    envelope = json.loads(cache.read_text())
    envelope["mac"] = "0" * 64
    cache.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ReleaseRejected, match="INTEGRITY"):
        refresh_workspace(workspace, project[3])
    failed = workspace_status(workspace, project[3])
    assert failed["status"] == "failed" and failed["risk_count"] is None
    repaired = refresh_workspace(workspace, project[3], force=True)
    assert repaired["status"] == "ready" and repaired["risk_count"] == 2
    assert semantic(repaired) == semantic(refresh_workspace(workspace, project[3]))


def test_offline_core_does_not_open_network_sockets(project, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("network use in local workspace")
    monkeypatch.setattr(socket, "socket", network_forbidden)
    workspace = start(project)
    assert refresh_workspace(workspace, project[3])["status"] == "ready"
    assert prepare_workspace(workspace, project[3])["state"] == "VALIDATED"


def test_headless_cli_never_reveals_private_view_and_real_entry_refreshes(project):
    created = subprocess.run([sys.executable, "-m", "privacyfs.cli", "workspace", "init", str(project[0]),
        "--relations", str(project[1]), "--task-profile", str(project[2]), "--state-dir", str(project[3])], capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    report = json.loads(created.stdout)
    assert report["risk_count"] == 2
    result = CliRunner().invoke(app, ["workspace", "review", report["workspace_id"], "--local", "--state-dir", str(project[3])])
    assert result.exit_code == 3
    assert "LOCAL_TERMINAL_REQUIRED" in result.stdout
    assert "示例企业甲" not in result.stdout and str(project[0]) not in result.stdout


def test_same_mtime_change_is_detected_and_failed_status_is_inspectable(project):
    workspace = start(project)
    before = refresh_workspace(workspace, project[3])
    path = project[0] / "event.md"
    info = path.stat()
    path.write_bytes(b"mid=M01;status=ended\n")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    after = refresh_workspace(workspace, project[3])
    assert after["revision"] > before["revision"]
    path.unlink()
    with pytest.raises(ProcessingStopped):
        refresh_workspace(workspace, project[3])
    status = workspace_status(workspace, project[3])
    assert status["status"] == "failed" and status["risk_count"] is None and not status["source_checked"]


def test_quasi_population_dependency_rebuilds_without_shared_key(project):
    config = json.loads(project[1].read_text())
    fields = [{"name": "cid", "role": "key", "namespace": "customer", "entity_type": "client"},
              {"name": "city", "role": "quasi"}, {"name": "job", "role": "quasi"}]
    for i in (1, 2):
        (project[0] / f"q{i}.csv").write_bytes(f"cid,city,job\nQ{i},city,job\n".encode())
        config["files"].append({"path": f"q{i}.csv", "primary": "cid", "fields": fields})
    project[1].write_text(json.dumps(config), encoding="utf-8")
    workspace = start(project)
    initial = refresh_workspace(workspace, project[3])
    assert [r["support"] for r in initial["risks"] if r["rule"] == "quasi_combination"] == [2, 2]
    (project[0] / "q2.csv").write_bytes(b"cid,city,job\nQ2,other,job\n")
    update = refresh_workspace(workspace, project[3])
    assert [r["support"] for r in update["risks"] if r["rule"] == "quasi_combination"] == [1, 1]
    assert update["metrics"]["rebuilt_components"] == 1
    assert semantic(update) == semantic(refresh_workspace(workspace, project[3], force=True))


def test_workspace_ids_do_not_allow_corrections_to_cross_projects(project, tmp_path):
    from privacyfs.state_store import create_workspace
    import shutil
    first = start(project)
    one = refresh_workspace(first, project[3])
    other = tmp_path / "other-source"
    shutil.copytree(project[0], other)
    second = create_workspace(other, other / "relations.json", other / "profile.json", project[3])["workspace_id"]
    two = refresh_workspace(second, project[3])
    assert {e["entity_id"] for e in one["entities"]}.isdisjoint(e["entity_id"] for e in two["entities"])
    with pytest.raises(ValueError, match="unknown entity"):
        correct_workspace(first, project[3], "confirm", [two["entities"][0]["entity_id"]], expected_revision=one["revision"])
    assert workspace_status(first, project[3])["revision"] == one["revision"]


def test_interrupted_forget_can_complete_after_index_deletion(project, monkeypatch):
    from privacyfs.review import forget_workspace, list_workspaces
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    original = Path.unlink
    def stop_after_index(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == "index.json":
            raise SystemExit("synthetic cleanup interruption")
        return result
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", stop_after_index)
        with pytest.raises(SystemExit):
            forget_workspace(workspace, project[3], expected_revision=report["revision"])
    assert list_workspaces(project[3])["workspaces"][0]["status"] == "interrupted_init"
    assert forget_workspace(workspace, project[3], expected_revision=report["revision"])["status"] == "forgotten"


def test_cleanup_preserves_foreign_directory(project, tmp_path):
    from privacyfs.review import forget_workspace
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    # A foreign extra directory must never be recursively removed by cleanup.
    foreign = project[3] / "workspaces" / workspace / "cache" / "foreign"
    foreign.mkdir()
    (foreign / "preserve.txt").write_bytes(b"preserve")
    with pytest.raises(ReleaseRejected, match="UNKNOWN_WORKSPACE_CONTENT"):
        forget_workspace(workspace, project[3], expected_revision=report["revision"])
    assert (foreign / "preserve.txt").read_bytes() == b"preserve"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_actual_cache_junction_is_not_read_or_removed_recursively(project, tmp_path):
    from privacyfs.review import forget_workspace
    workspace = start(project)
    refresh_workspace(workspace, project[3])
    store = WorkspaceStore(ReleaseStore(project[3]))
    record = store.load(workspace)
    path = store.cache_path(workspace, next(iter(record["files"].values()))["cache"])
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (outside / "preserve.txt").write_bytes(b"preserve")
    path.unlink()  # only an owned synthetic derivative cache
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(outside)], capture_output=True)
    assert made.returncode == 0
    try:
        with pytest.raises(ProcessingStopped, match="REPARSE_POINT"):
            refresh_workspace(workspace, project[3])
        revision = workspace_status(workspace, project[3])["revision"]
        with pytest.raises(ProcessingStopped, match="REPARSE_POINT"):
            forget_workspace(workspace, project[3], expected_revision=revision)
        assert (outside / "preserve.txt").read_bytes() == b"preserve"
    finally:
        os.rmdir(path)  # unlink the junction itself, never recurse into target


def test_unknown_and_conflicting_assertions_stay_visible_for_review(project):
    settings = json.loads(project[1].read_text())
    settings["files"][2]["state_field"] = "assertion"
    (project[0] / "event.md").write_bytes(b"mid=M01;status=secret;assertion=unknown\n")
    project[1].write_text(json.dumps(settings), encoding="utf-8")
    workspace = start(project)
    report = refresh_workspace(workspace, project[3])
    assert report["assertion_states"]["unknown"] == 1
    assert any(e["state"] == "unknown" for e in report["review_items"])
