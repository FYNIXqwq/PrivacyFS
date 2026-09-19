"""Measure exact structured-task utility and retained relation equivalence."""
from collections import Counter
from dataclasses import replace
from pathlib import Path
import argparse
import csv
import hashlib
import io
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from privacyfs.documents import DocumentSession
from privacyfs.evidence import EvidenceGraph
from privacyfs.planning import build_plan
from privacyfs.relations import FieldSpec, RecordTemplate
from privacyfs.task_profiles import load_task_profile
from privacyfs.verifier import verify_candidate


def load_dataset(path):
    data = path.read_bytes()
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("D3 frozen corpus hash mismatch")
    cases = [json.loads(line) for line in data.decode().splitlines()]
    groups, ids, texts = {}, set(), {}
    for case in cases:
        if case["id"] in ids or case["split"] not in {"development", "validation"}:
            raise ValueError("invalid case identity")
        ids.add(case["id"])
        if groups.setdefault(case["group"], case["split"]) != case["split"]:
            raise ValueError("task family crosses partitions")
        for doc in case["documents"]:
            if texts.setdefault(doc["text"], case["split"]) != case["split"]:
                raise ValueError("duplicate source crosses partitions")
    if len(cases) != manifest["cases"] or len(groups) != manifest["families"]:
        raise ValueError("manifest count mismatch")
    return cases, manifest


def actual_rows(artifact):
    text = artifact.payload.decode("utf-8")
    if artifact.format == "csv":
        return list(csv.DictReader(io.StringIO(text, newline="")))
    return [dict(part.split("=", 1) for part in line.split(";")) for line in text.splitlines()]


def run(path, split="validation"):
    cases, manifest = load_dataset(path)
    selected = [c for c in cases if split == "all" or c["split"] == split]
    counts, tasks, errors = Counter(), Counter(), []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="privacyfs-d3-eval-") as temp:
        for index, case in enumerate(selected):
            root = Path(temp) / str(index)
            root.mkdir()
            profile_file = root / "profile.json"
            profile_file.write_text(json.dumps(case["profile"]), encoding="utf-8")
            profile = load_task_profile(profile_file)
            with DocumentSession(root) as session:
                bindings = []
                for doc in case["documents"]:
                    (root / doc["path"]).write_bytes(doc["text"].encode("utf-8"))
                    specs = tuple(FieldSpec(**{**f, "values": tuple(f.get("values", ()))}) for f in doc["template"]["fields"])
                    template = RecordTemplate(specs, doc["template"]["primary"], doc["template"].get("state_field"))
                    bindings.append((session.parse(session.capture(doc["path"])), template))
                with EvidenceGraph(bindings) as graph:
                    plan = build_plan(bindings, graph, graph.analyze(), profile)
                    if plan.selected is None:
                        errors.append({"case": case["id"], "kind": "no_feasible_plan"})
                        continue
                    chosen = plan.candidates[plan.selected]
                    output = root / "synthetic-output"
                    output.mkdir()
                    actual = []
                    for artifact in chosen.artifacts:
                        file = output / artifact.filename
                        file.write_bytes(artifact.payload)
                        actual.append(replace(artifact, payload=file.read_bytes()))
                    verify_candidate(plan, replace(chosen, artifacts=tuple(actual)))
                    rows = [actual_rows(a) for a in actual]
                    facts = [r["fact"] for r in rows[2]]
                    correct = facts == case["expected_facts"] and Counter(facts) == Counter(case["expected_facts"])
                    correct = correct and all(r["assertion_state"] == case["expected_state"] for r in rows[2])
                    keys = {r["client_id"] for r in rows[0]}
                    correct = correct and all(r["client_id"] in keys for r in rows[1])
                    correct = correct and [r["meeting_id"] for r in rows[1]] == [r["meeting_id"] for r in rows[2]]
                    body = b"".join(a.payload for a in actual).decode("utf-8")
                    if any(value in body for value in case["forbidden"]):
                        errors.append({"case": case["id"], "kind": "gold_residual"})
                    if not correct:
                        errors.append({"case": case["id"], "kind": "task_or_relationship_changed"})
                    if chosen.loss >= plan.candidates[-1].loss or plan.candidates[-1].feasible:
                        errors.append({"case": case["id"], "kind": "suppression_baseline"})
                    for doc in case["documents"]:
                        if (root / doc["path"]).read_bytes() != doc["text"].encode("utf-8"):
                            errors.append({"case": case["id"], "kind": "source_changed"})
                    counts["validated_artifacts"] += len(actual)
                    counts["preserved_fact_rows"] += len(facts)
                    counts["exact_task_matches"] += bool(correct)
                    counts["better_than_full_suppression"] += chosen.loss < plan.candidates[-1].loss
                    tasks[profile.task] += 1
    return {"dataset": manifest["version"], "sha256": manifest["sha256"], "split": split,
            "cases": len(selected), "counts": dict(counts), "tasks": dict(tasks), "errors": errors,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "scope": "synthetic_structured_task_bytes_not_semantic_summary_quality_or_runtime_isolation"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("all", "development", "validation"), default="validation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(Path(__file__).parent / "data/d3-v1.jsonl", args.split)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(1 if result["errors"] else 0)
