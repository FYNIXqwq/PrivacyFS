"""Synthetic D3 prepare/approve/export benchmark with real private state."""
from pathlib import Path
import argparse
import json
import os
import platform
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from privacyfs.release import prepare_release, review_release, export_release, show_release
from evidence_d2 import peak_rss


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subjects", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.subjects <= 300:
        raise SystemExit("subjects must be 1..300")
    with tempfile.TemporaryDirectory(prefix="privacyfs-d3-benchmark-") as temp:
        root = Path(temp) / "source"
        root.mkdir()
        (root / "people.csv").write_bytes(("pid,name\n" + "".join(f"P{i},Synthetic{i}\n" for i in range(args.subjects))).encode())
        (root / "facts.csv").write_bytes(("pid,fact\n" + "".join(f"P{i},ready\n" for i in range(args.subjects))).encode())
        key = {"name": "pid", "role": "key", "namespace": "people", "entity_type": "person"}
        relations = {"schema_version": "d2-templates-1", "files": [
            {"path": "people.csv", "primary": "pid", "fields": [key, {"name": "name", "role": "identity"}]},
            {"path": "facts.csv", "primary": "pid", "fields": [key, {"name": "fact", "role": "sensitive", "values": ["ready"]}]}]}
        profile = {"schema_version": "d3-task-profile-1", "task": "record_summary", "recipient": "synthetic-local",
                   "fields": [{"name": "fact", "output_name": "fact", "approved_values": ["ready"]}]}
        (root / "relations.json").write_text(json.dumps(relations), encoding="utf-8")
        (root / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
        started = time.perf_counter()
        state, destination = Path(temp) / "private", Path(temp) / "published"
        prepared = prepare_release(root, root / "relations.json", root / "profile.json", state)
        planned_at = time.perf_counter()
        review_release(prepared["plan_id"], state, approve=prepared["approval_digest"])
        approved_at = time.perf_counter()
        published = export_release(prepared["plan_id"], destination, state)
        exported_at = time.perf_counter()
        assert published["state"] == "PUBLISHED"
        show_release(prepared["release_id"], state)
        output = {"subjects": args.subjects, "files": 2, "fact_rows": args.subjects,
                  "prepare_seconds": round(planned_at - started, 3), "approve_seconds": round(approved_at - planned_at, 3),
                  "export_seconds": round(exported_at - approved_at, 3), "total_seconds": round(exported_at - started, 3),
                  "peak_process_rss_mib": round(peak_rss() / 1048576, 2), "python": platform.python_version(),
                  "platform": platform.platform(), "logical_cpu": os.cpu_count(),
                  "scope": "small_synthetic_full_local_publication_no_models_no_human_review_time"}
        text = json.dumps(output, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
