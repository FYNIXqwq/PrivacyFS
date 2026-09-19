"""Frozen D2 linkage-pair metrics, per-rule case metrics and source replay."""
from collections import Counter
from itertools import combinations
from pathlib import Path
import argparse
import hashlib
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from privacyfs.documents import DocumentSession
from privacyfs.evidence import EvidenceGraph, public_graph_report
from privacyfs.relations import FieldSpec, RecordTemplate


def load_dataset(path):
    payload = path.read_bytes()
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
        raise ValueError("frozen D2 corpus hash mismatch")
    cases = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
    groups, ids, texts = {}, set(), {}
    for case in cases:
        if case["id"] in ids or case["split"] not in {"development", "validation"}:
            raise ValueError("invalid case identity")
        ids.add(case["id"])
        if groups.setdefault(case["group"], case["split"]) != case["split"]:
            raise ValueError("template family crosses partitions")
        for doc in case["documents"]:
            if texts.setdefault(doc["text"], case["split"]) != case["split"]:
                raise ValueError("identical document crosses partitions")
    if len(cases) != manifest["cases"] or len(groups) != manifest["families"]:
        raise ValueError("manifest count mismatch")
    return cases, manifest


def metrics(tp=0, fp=0, fn=0):
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


def run(path, split="all"):
    cases, manifest = load_dataset(path)
    selected = [c for c in cases if split == "all" or c["split"] == split]
    counts, links, by_rule, errors = Counter(), Counter(), {}, []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="privacyfs-d2-eval-") as temporary:
        for index, case in enumerate(selected):
            root = Path(temporary) / str(index)
            root.mkdir()
            with DocumentSession(root) as session:
                bindings, doc_names = [], {}
                for doc in case["documents"]:
                    file = root / doc["path"]
                    # Data is frozen synthetic content; preserve literal LF.
                    file.write_bytes(doc["text"].encode("utf-8"))
                    parsed = session.parse(session.capture(doc["path"]))
                    fields = tuple(FieldSpec(**{**s, "values": tuple(s.get("values", ()))}) for s in doc["template"]["fields"])
                    template = RecordTemplate(fields, doc["template"]["primary"], doc["template"].get("state_field"))
                    bindings.append((parsed, template))
                    doc_names[parsed.snapshot.document_id] = doc["path"]
                with EvidenceGraph(bindings) as graph:
                    result = graph.analyze(quasi_fields=tuple(case["quasi_fields"]))
                    report = public_graph_report(graph, result)
                    predicted = Counter(w.rule for w in result.witnesses)
                    for rule, expected in case["expected_risks"].items():
                        bucket = by_rule.setdefault(rule, Counter())
                        bucket["tp"] += bool(expected) and bool(predicted[rule])
                        bucket["fp"] += not expected and bool(predicted[rule])
                        bucket["fn"] += bool(expected) and not predicted[rule]
                        bucket["tn"] += not expected and not predicted[rule]
                        if predicted[rule] != expected:
                            errors.append({"case": case["id"], "kind": "risk_count", "rule": rule,
                                           "expected": expected, "actual": predicted[rule]})
                    gold = {(o["path"], o["start"], o["end"]): o["entity"] for o in case["occurrences"]}
                    actual = {}
                    for oid, occurrence in graph.resolver.occurrences.items():
                        loc = occurrence.locator
                        actual[doc_names[loc.document_id], loc.start, loc.end] = graph.resolver.assignments[oid]
                    if actual.keys() != gold.keys():
                        errors.append({"case": case["id"], "kind": "key_location"})
                    expected_pairs = {tuple(sorted((a, b))) for a, b in combinations(gold, 2) if gold[a] == gold[b]}
                    actual_pairs = {tuple(sorted((a, b))) for a, b in combinations(actual, 2) if actual[a] == actual[b]}
                    links["tp"] += len(expected_pairs & actual_pairs)
                    links["fp"] += len(actual_pairs - expected_pairs)
                    links["fn"] += len(expected_pairs - actual_pairs)
                    if expected_pairs != actual_pairs:
                        errors.append({"case": case["id"], "kind": "entity_link_pairs"})
                    if report["incremental_numbered_joins"] != case["expected_incremental_joins"]:
                        errors.append({"case": case["id"], "kind": "baseline_increment"})
                    counts["incremental_numbered_joins"] += report["incremental_numbered_joins"]
                    for witness in result.witnesses:
                        for eid in witness.evidence_ids:
                            detail = graph.local_evidence(result, eid)
                            for source in detail["sources"]:
                                for loc, text in zip(source["locations"], source["source_text"]):
                                    original = next(d["text"] for d in case["documents"] if d["path"] == doc_names[loc.document_id])
                                    if original[loc.start:loc.end] != text:
                                        errors.append({"case": case["id"], "kind": "source_replay"})
                                    counts["source_locations_replayed"] += 1
                    counts["witnesses"] += len(result.witnesses)
                    for doc in case["documents"]:
                        if (root / doc["path"]).read_bytes() != doc["text"].encode("utf-8"):
                            errors.append({"case": case["id"], "kind": "source_changed"})
                    counts["documents"] += len(bindings)
    return {"dataset": manifest["version"], "sha256": manifest["sha256"], "split": split,
            "cases": len(selected), "counts": dict(counts), "link_pairs": metrics(**links),
            "risk_case_metrics": {rule: {**metrics(c["tp"], c["fp"], c["fn"]), "tn": c["tn"]} for rule, c in by_rule.items()},
            "errors": errors, "elapsed_seconds": round(time.perf_counter() - started, 3),
            "scope": "synthetic_explicit_templates_not_general_entity_or_semantic_accuracy"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("all", "development", "validation"), default="validation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(Path(__file__).parent / "data" / "d2-v1.jsonl", args.split)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    raise SystemExit(1 if result["errors"] else 0)
