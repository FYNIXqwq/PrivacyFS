"""Validate the frozen D1 corpus and emit aggregate metrics without source text."""
from pathlib import Path
from collections import Counter
import argparse
import codecs
import hashlib
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from privacyfs.config import Rules
from privacyfs.documents import DocumentSession, ParseStatus, RuleInspector
from privacyfs.normalization import NORMALIZATION_VERSION, normalize_with_map
from privacyfs.paths import relative_parts


def load_dataset(path: Path):
    payload = path.read_bytes()
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
        raise ValueError("frozen dataset hash mismatch")
    cases = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
    groups, ids, inputs = {}, set(), {}
    for case in cases:
        if case["id"] in ids or case["split"] not in {"development", "validation"}:
            raise ValueError("invalid case or split")
        ids.add(case["id"])
        if groups.setdefault(case["group"], case["split"]) != case["split"]:
            raise ValueError("template family crosses splits")
        for document in case["documents"]:
            normalized = normalize_with_map(document["text"]).text
            signature = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if inputs.setdefault(signature, case["split"]) != case["split"]:
                raise ValueError("duplicate normalized input crosses splits")
    return cases, manifest


def minimal_rules(settings):
    rules = Rules(keywords=[], regexes=[], literal_names=[], literal_orgs=[], professions=[],
                  identity_terms=[], org_suffixes=[], use_ner=False, detect_pinyin=False, detect_en_names=False)
    for key, value in settings.items():
        if key not in {"literal_names", "literal_orgs", "keywords", "regexes"}:
            raise ValueError("unsupported evaluation rule")
        setattr(rules, key, value)
    return rules


def run(path: Path, split="all"):
    cases, manifest = load_dataset(path)
    selected = [c for c in cases if split == "all" or c["split"] == split]
    metrics, failures = Counter(), []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="privacyfs-d1-eval-") as temporary:
        base = Path(temporary)
        for index, case in enumerate(selected):
            root = base / str(index)
            root.mkdir()
            rules = minimal_rules(case["rules"])
            inspector = RuleInspector(rules)
            with DocumentSession(root) as session:
                for document in case["documents"]:
                    filename = root.joinpath(*relative_parts(document["path"]))
                    filename.parent.mkdir(parents=True, exist_ok=True)
                    encoding = document.get("encoding", "utf-8")
                    payload = document["text"].encode(encoding)
                    if document.get("bom"):
                        payload = {"utf-16-le": codecs.BOM_UTF16_LE}[encoding] + payload
                    filename.write_bytes(payload)
                    snapshot = session.capture(filename.relative_to(root))
                    parsed = session.parse(snapshot, encoding=document.get("explicit_encoding"))
                    metrics["documents"] += 1
                    matched = parsed.coverage.status.value == document["status"]
                    metrics["coverage_correct"] += matched
                    if not matched: failures.append(case["id"] + ":coverage")
                    if parsed.coverage.status is not ParseStatus.COMPLETE:
                        continue
                    findings = inspector.inspect(parsed)
                    actual = {(f.category, f.locator.start, f.locator.end) for f in findings}
                    expected = {(f["category"], f["start"], f["end"]) for f in document["findings"]}
                    metrics["tp"] += len(actual & expected)
                    metrics["fp"] += len(actual - expected)
                    metrics["fn"] += len(expected - actual)
                    if actual != expected: failures.append(case["id"] + ":localization")
                    for finding in findings:
                        located = parsed.resolve(finding.locator)
                        if located != parsed.text[finding.locator.start:finding.locator.end]:
                            failures.append(case["id"] + ":source-map")
                        metrics["locators_checked"] += 1
                    if parsed.text != document["text"] or filename.read_bytes() != payload:
                        failures.append(case["id"] + ":source-preservation")
                    if document.get("expected_rows") is not None:
                        metrics["table_tasks"] += 1
                        if parsed.csv_rows != document["expected_rows"]:
                            failures.append(case["id"] + ":table-preservation")
    tp, fp, fn = (metrics[key] for key in ("tp", "fp", "fn"))
    return {
        "dataset": manifest["version"], "dataset_sha256": manifest["sha256"],
        "scope": manifest["scope"], "split": split, "cases": len(selected),
        "case_kinds": dict(Counter(c["kind"] for c in selected)),
        "normalization_version": NORMALIZATION_VERSION,
        "metrics": dict(metrics), "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "seconds": round(time.perf_counter() - started, 3),
        "failures": failures, "passed": not failures,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parent / "data/d1-v2.jsonl")
    parser.add_argument("--split", choices=["all", "development", "validation"], default="all")
    args = parser.parse_args()
    result = run(args.dataset, args.split)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__": main()
