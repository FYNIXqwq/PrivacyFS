"""D4 gold-rule regression and cold/warm/change/full equivalence on frozen D2 data."""
from collections import Counter
from pathlib import Path
import argparse
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from run_d2 import load_dataset
from privacyfs.release import ReleaseStore
from privacyfs.state_store import create_workspace
from privacyfs.incremental import WorkspaceView


def semantic(report):
    return {k: report[k] for k in ("revision", "status", "risks", "entities", "candidates", "corrections")}


def run(split="validation"):
    cases, manifest = load_dataset(Path(__file__).parent / "data/d2-v1.jsonl")
    selected = [c for c in cases if split == "all" or c["split"] == split]
    counts, errors = Counter(), []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="privacyfs-d4-eval-") as temporary:
        base = Path(temporary)
        for index, case in enumerate(selected):
            source, state = base / str(index), base / (str(index) + "-state")
            source.mkdir()
            config = {"schema_version": "d2-templates-1", "files": []}
            for doc in case["documents"]:
                (source / doc["path"]).write_bytes(doc["text"].encode("utf-8"))
                config["files"].append({"path": doc["path"], **doc["template"]})
            fact = next(f for d in case["documents"] for f in d["template"]["fields"] if f["role"] == "sensitive")
            profile = {"schema_version": "d3-task-profile-1", "task": "record_summary", "recipient": "synthetic",
                       "fields": [{"name": fact["name"], "output_name": "fact", "approved_values": fact["values"]}]}
            config_path, profile_path = source / "relations.json", source / "profile.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            workspace = create_workspace(source, config_path, profile_path, state)["workspace_id"]
            releases = ReleaseStore(state)
            phases = {}
            for phase in ("cold", "warm", "changed", "full"):
                if phase == "changed":
                    for doc in case["documents"]:
                        if doc["path"].startswith(("bridge0.", "copy0.")):
                            at = next(o["start"] for o in case["occurrences"] if o["path"] == doc["path"])
                            (source / doc["path"]).write_bytes((doc["text"][:at] + "DISCONNECTED_" + doc["text"][at:]).encode("utf-8"))
                with releases.lock(), WorkspaceView(releases, workspace, force=phase == "full") as view:
                    report = view.public()
                phases[phase] = report
                counts["refreshes"] += 1
                counts["parse_cache_hits"] += report["metrics"].get("parse_cache_hits", 0)
                counts["graph_cache_hits"] += report["metrics"].get("graph_cache_hits", 0)
            if semantic(phases["cold"]) != semantic(phases["warm"]):
                errors.append({"case": case["id"], "kind": "warm_differs"})
            if semantic(phases["changed"]) != semantic(phases["full"]):
                errors.append({"case": case["id"], "kind": "incremental_differs"})
            actual = Counter(w["rule"] for w in phases["cold"]["risks"])
            if any(actual[rule] != value for rule, value in case["expected_risks"].items()):
                errors.append({"case": case["id"], "kind": "gold_rules"})
            if any(w["rule"] == "numbered_join" for w in phases["changed"]["risks"]):
                errors.append({"case": case["id"], "kind": "changed_bridge_still_connected"})
            if phases["warm"]["metrics"].get("rebuilt_components", 0) or phases["warm"]["metrics"].get("parsed_documents", 0):
                errors.append({"case": case["id"], "kind": "warm_cache_missed"})
            counts["cases"] += 1
            if (index + 1) % 20 == 0:
                print(json.dumps({"completed_cases": index + 1}), file=sys.stderr, flush=True)
    return {"source_dataset": manifest["version"], "sha256": manifest["sha256"], "split": split,
            "counts": dict(counts), "errors": errors, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "scope": "regression_reusing_frozen_synthetic_D2_not_new_independent_accuracy_evaluation"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("all", "development", "validation"), default="validation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.split)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(1 if result["errors"] else 0)
