"""D4 cache reuse must preserve source/version semantics, not just speed."""
import json
import threading

import pytest

from privacyfs.release import ReleaseStore
from privacyfs.state_store import create_workspace
from privacyfs.incremental import WorkspaceView
from privacyfs.documents.models import ProcessingStopped
from test_release_lifecycle import project


def start(project):
    return create_workspace(*project[:4])["workspace_id"]


def refresh(project, workspace_id, **kwargs):
    store = ReleaseStore(project[3])
    with store.lock(), WorkspaceView(store, workspace_id, **kwargs) as view:
        return view.public()


def semantic(report):
    return {k: report[k] for k in ("workspace_id", "revision", "status", "risks", "entities", "candidates", "corrections")}


def test_cold_warm_and_forced_full_are_equivalent(project):
    workspace = start(project)
    cold = refresh(project, workspace)
    warm = refresh(project, workspace)
    full = refresh(project, workspace, force=True)
    assert cold["metrics"]["parsed_documents"] == 3
    assert warm["metrics"]["parse_cache_hits"] == 3
    assert warm["metrics"]["graph_cache_hits"] == 1
    assert warm["metrics"].get("rebuilt_components", 0) == 0
    assert semantic(cold) == semantic(warm) == semantic(full)
    assert cold["risk_count"] == 2


def test_unrelated_change_only_rebuilds_its_component(project):
    config = json.loads(project[1].read_text())
    extra = {"path": "extra.csv", "primary": "id", "fields": [
        {"name": "id", "role": "key", "namespace": "unrelated", "entity_type": "person"},
        {"name": "name", "role": "identity"}]}
    config["files"].append(extra)
    project[1].write_text(json.dumps(config), encoding="utf-8")
    (project[0] / "extra.csv").write_bytes(b"id,name\nX1,SyntheticAlpha\n")
    workspace = start(project)
    first = refresh(project, workspace)
    (project[0] / "extra.csv").write_bytes(b"id,name\nX2,SyntheticBeta\n")
    update = refresh(project, workspace)
    assert update["revision"] == first["revision"] + 1
    assert update["metrics"]["parsed_documents"] == 1
    assert update["metrics"]["rebuilt_components"] == 1
    assert update["metrics"]["graph_cache_hits"] == 1
    assert semantic(update) == semantic(refresh(project, workspace, force=True))


def test_cancelled_refresh_does_not_commit_a_partial_revision(project):
    workspace = start(project)
    before = refresh(project, workspace)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProcessingStopped, match="CANCELLED"):
        refresh(project, workspace, cancel=cancel)
    assert semantic(before) == semantic(refresh(project, workspace))
