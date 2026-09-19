"""Authorized synthetic D4 demonstration. No original examples are modified."""
from pathlib import Path
import hashlib
import json
import shutil

from privacyfs.state_store import create_workspace
from privacyfs.review import (refresh_workspace, prepare_workspace, local_workspace_details,
    correct_workspace, undo_workspace, run_local_console, forget_workspace)
from privacyfs.release import review_release, export_release, show_release

root = Path(__file__).resolve().parents[1]
base = root / ".review/d4-demo"
base.mkdir(exist_ok=False)
source, state, output = base / "source", base / "private", base / "output"
shutil.copytree(root / "examples/d3/statistics", source)
workspace = create_workspace(source, source / "relations.json", source / "profile.json", state)["workspace_id"]
events = []
report = refresh_workspace(workspace, state)
events.append({"stage": "initial", "revision": report["revision"], "risks": report["risk_count"]})
prepared = prepare_workspace(workspace, state)
review_release(prepared["plan_id"], state, approve=prepared["approval_digest"])
local = local_workspace_details(workspace, state, expected_revision=report["revision"])
occurrence = next(o for o in local["occurrences"] if o["path"].endswith("meetings.csv") and o["value"] == "C001")
split = correct_workspace(workspace, state, "split", [occurrence["entity_id"], occurrence["occurrence_id"]], expected_revision=report["revision"])
assert review_release(prepared["plan_id"], state)["state"] == "NEEDS_REVIEW"
events.append({"stage": "split", "revision": split["revision"], "risks": split["risk_count"], "old_approval": "NEEDS_REVIEW"})
undone = undo_workspace(workspace, state, expected_revision=split["revision"])
assert undone["risk_count"] == report["risk_count"]
commands = iter(["sources", "occurrences", "plan", "quit"])
display = {"lines": 0, "commands": 0}
def read(_):
    display["commands"] += 1
    return next(commands)
def write(_):
    display["lines"] += 1
run_local_console(workspace, state, read=read, write=write)
events.append({"stage": "local_review", **display})
settings = json.loads((source / "relations.json").read_text(encoding="utf-8"))
entry = dict(settings["files"][-1])
entry["path"] = "extra.txt"
settings["files"].append(entry)
(source / "extra.txt").write_bytes("会议编号=M003;合作状态=终止合作\n".encode("utf-8"))
(source / "relations.json").write_text(json.dumps(settings), encoding="utf-8")
incremental = refresh_workspace(workspace, state)
events.append({"stage": "add_unrelated", "revision": incremental["revision"], "metrics": incremental["metrics"]})
assert incremental["metrics"]["rebuilt_components"] == 1
plan = prepare_workspace(workspace, state)
before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
review_release(plan["plan_id"], state, approve=plan["approval_digest"])
published = export_release(plan["plan_id"], output, state)
assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
events.append({"stage": "publish", "state": published["state"], "release_id": published["release_id"]})
forget_workspace(workspace, state, expected_revision=incremental["revision"])
assert show_release(plan["release_id"], state)["state"] == "PUBLISHED"
events.append({"stage": "forget", "workspace_exists": (state / "workspaces" / workspace).exists(), "publication_preserved": True})
(root / ".review/d4-demo-results.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
print(json.dumps(events, indent=2))
