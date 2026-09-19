"""Frozen D3 task texts converted to bounded DOCX/PDF input projections."""
from collections import Counter
from dataclasses import replace
from pathlib import Path
import argparse
import csv
import io
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from run_d3 import load_dataset, actual_rows
from privacyfs.documents import DocumentSession, ParseStatus
from privacyfs.evidence import EvidenceGraph
from privacyfs.planning import build_plan
from privacyfs.relations import FieldSpec, RecordTemplate
from privacyfs.sample_documents import docx_bytes, pdf_bytes
from privacyfs.task_profiles import load_task_profile
from privacyfs.verifier import verify_candidate


def run(split="validation"):
    cases, manifest = load_dataset(Path(__file__).parent / "data/d3-v1.jsonl")
    cases = [c for c in cases if split == "all" or c["split"] == split]
    metrics, errors = Counter(), []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="privacyfs-d5-eval-") as temp:
        for index, case in enumerate(cases):
            root = Path(temp) / str(index)
            root.mkdir()
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(case["profile"]), encoding="utf-8")
            profile = load_task_profile(profile_path)
            originals = {}
            with DocumentSession(root) as session:
                bindings = []
                for number, doc in enumerate(case["documents"]):
                    specs = tuple(FieldSpec(**{**f, "values": tuple(f.get("values", ()))}) for f in doc["template"]["fields"])
                    if doc["path"].endswith(".csv"):
                        name, payload = f"{number}.docx", docx_bytes(tables=(list(csv.reader(io.StringIO(doc["text"], newline=""))),))
                        template = RecordTemplate(specs, doc["template"]["primary"], doc["template"].get("state_field"), source_view="docx_table", table_index=0)
                    else:
                        name, payload = f"{number}.pdf", pdf_bytes([doc["text"]], attachment=True)
                        template = RecordTemplate(specs, doc["template"]["primary"], doc["template"].get("state_field"), source_view="pdf_pages", pages=(1,))
                    (root / name).write_bytes(payload)
                    originals[name] = payload
                    parsed = session.parse(session.capture(name))
                    if parsed.coverage.status != ParseStatus.PARTIAL or not parsed.coverage.projection_complete:
                        errors.append({"case": case["id"], "kind": "projection_coverage"})
                    bindings.append((parsed, template))
                    metrics["documents"] += 1
                with EvidenceGraph(bindings) as graph:
                    analysis = graph.analyze()
                    for finding in analysis.evidence:
                        for aid in finding.assertion_ids:
                            for loc in graph._raw[aid].locators:
                                document = graph._documents[loc.document_id]
                                assert document.resolve(loc) == document.text[loc.start:loc.end]
                                if loc.unit != "extracted_unicode_codepoint" or loc.part is None:
                                    errors.append({"case": case["id"], "kind": "source_location"})
                                metrics["locators_replayed"] += 1
                    plan = build_plan(bindings, graph, analysis, profile)
                    if plan.selected is None:
                        errors.append({"case": case["id"], "kind": "no_feasible_plan"})
                        continue
                    candidate = plan.candidates[plan.selected]
                    verify_candidate(plan, candidate)
                    actual = [actual_rows(a) for a in candidate.artifacts]
                    if [row["fact"] for row in actual[2]] != case["expected_facts"] or any(row["assertion_state"] != case["expected_state"] for row in actual[2]):
                        errors.append({"case": case["id"], "kind": "task_changed"})
                    if [row["meeting_id"] for row in actual[1]] != [row["meeting_id"] for row in actual[2]]:
                        errors.append({"case": case["id"], "kind": "relationship_changed"})
                    output = root / "output"
                    output.mkdir()
                    combined = b""
                    for artifact in candidate.artifacts:
                        if artifact.format != "csv":
                            errors.append({"case": case["id"], "kind": "unexpected_format"})
                        path = output / artifact.filename
                        path.write_bytes(artifact.payload)
                        combined += path.read_bytes()
                    for value in [*case["forbidden"], "SYNTHETIC_PRIVATE_HEADER", "SYNTHETIC_PRIVATE_AUTHOR", "SYNTHETIC_PRIVATE_ATTACHMENT"]:
                        if value.encode("utf-8") in combined:
                            errors.append({"case": case["id"], "kind": "residual"})
                    metrics["verified_csv_files"] += len(candidate.artifacts)
                    metrics["preserved_fact_rows"] += len(actual[2])
            if any((root / name).read_bytes() != payload for name, payload in originals.items()):
                errors.append({"case": case["id"], "kind": "source_changed"})
            metrics["cases"] += 1
            if (index + 1) % 10 == 0:
                print(json.dumps({"completed_cases": index + 1}), file=sys.stderr, flush=True)
    return {"source_dataset": manifest["version"], "sha256": manifest["sha256"], "split": split,
            "conversion_version": "d5-docx-table-pdf-page-v1", "metrics": dict(metrics), "errors": errors,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "scope": "synthetic_structured_projections_not_real_world_Office_or_PDF_anonymity"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("all", "development", "validation"), default="validation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.split)
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    raise SystemExit(1 if result["errors"] else 0)
