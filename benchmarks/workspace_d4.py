"""D4 cold/warm/unrelated-add benchmarks over the same checked SDK store."""
from pathlib import Path
import argparse
import json
import os
import platform
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from privacyfs.release import ReleaseStore
from privacyfs.state_store import create_workspace
from privacyfs.incremental import WorkspaceView
from evidence_d2 import peak_rss


def run(files=40, kib=24):
    with tempfile.TemporaryDirectory(prefix="privacyfs-d4-benchmark-") as temp:
        root, state = Path(temp) / "source", Path(temp) / "private"
        root.mkdir()
        bindings = []
        for index in range(files):
            name = f"{index}.csv"
            (root / name).write_bytes((f"id,name,padding\nK{index},Synthetic{index}," + "x" * (kib * 1024) + "\n").encode())
            bindings.append({"path": name, "primary": "id", "fields": [
                {"name": "id", "role": "key", "namespace": f"namespace{index}", "entity_type": "client"},
                {"name": "name", "role": "identity"}]})
        # An approved profile is required for the workspace configuration, but
        # this benchmark times indexing/review, not D3 artifact publishing.
        profile = {"schema_version": "d3-task-profile-1", "task": "record_summary", "recipient": "synthetic",
                   "fields": [{"name": "name", "output_name": "name", "approved_values": ["synthetic"]}]}
        profile_path, config_path = root / "profile.json", root / "relations.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        config_path.write_text(json.dumps({"schema_version": "d2-templates-1", "files": bindings}), encoding="utf-8")
        workspace = create_workspace(root, config_path, profile_path, state)["workspace_id"]
        releases = ReleaseStore(state)
        results = {}
        for phase in ("cold", "warm", "add_unrelated", "forced_full"):
            if phase == "add_unrelated":
                (root / "new.csv").write_bytes(b"id,name\nNEW,SyntheticNew\n")
                bindings.append({"path": "new.csv", "primary": "id", "fields": [
                    {"name": "id", "role": "key", "namespace": "new", "entity_type": "client"},
                    {"name": "name", "role": "identity"}]})
                config_path.write_text(json.dumps({"schema_version": "d2-templates-1", "files": bindings}), encoding="utf-8")
            started = time.perf_counter()
            with releases.lock(), WorkspaceView(releases, workspace, force=phase == "forced_full") as view:
                public = view.public()
            results[phase] = {"seconds": round(time.perf_counter() - started, 4), **public["metrics"]}
        return {"files_initial": files, "input_mib": round(files * kib / 1024, 2), "phases": results,
                "unrelated_add_fraction_of_full": round(results["add_unrelated"]["seconds"] / results["forced_full"]["seconds"], 4),
                "peak_rss_mib": round(peak_rss() / 1048576, 2), "python": platform.python_version(),
                "platform": platform.platform(), "logical_cpu": os.cpu_count(),
                "scope": "synthetic_SDK_indexing_includes_bytes_cache_crypto_and_risks_excludes_store_open_and_human_review"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=40)
    parser.add_argument("--kib", type=int, default=24)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.files <= 100 or not 1 <= args.kib <= 64:
        raise SystemExit("benchmark limits exceeded")
    result = run(args.files, args.kib)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
