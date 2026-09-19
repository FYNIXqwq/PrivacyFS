"""Read-only QA plus a new explicit change manifest for this non-Git task."""
from pathlib import Path
import ast
import hashlib
import json

root = Path(__file__).resolve().parents[1]
files = [
    "tests/test_effectiveness_eval.py", "evals/build_effectiveness_dataset.py", "evals/run_effectiveness.py",
    "evals/effectiveness/__init__.py", "evals/effectiveness/adapters.py", "evals/effectiveness/attacks.py",
    "evals/effectiveness/metrics.py", "evals/effectiveness/runner.py",
    "evals/data/effectiveness-v1.jsonl", "evals/data/effectiveness-v1.manifest.json",
    "evals/pi/attack-harness.mjs", "evals/pi/attack-harness.test.mjs", "evals/pi/run-mock.mjs", "evals/pi/pi-example.mjs",
    "EVALUATION_FRAMEWORK.md", "EF1_IMPLEMENTATION_REPORT.md", "evals/README.md",
    "README.md", "user_manual.md", "AGENTS.md", "DEVELOPMENT_PLAN.md", ".github/workflows/tests.yml",
]
for name in files:
    path = root / name
    if path.suffix == ".py":
        ast.parse(path.read_text(encoding="utf-8"))
    if path.suffix == ".md":
        assert sum(line.startswith("```") for line in path.read_text(encoding="utf-8").splitlines()) % 2 == 0, name
for manifest_path in (root / "evals/data").glob("*.manifest.json"):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus = manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".jsonl"))
    assert hashlib.sha256(corpus.read_bytes()).hexdigest() == manifest["sha256"], corpus.name
for version in ("310", "311", "312"):
    log = (root / f".review/ef1-tests-py{version}.txt").read_text(encoding="utf-8-sig")
    assert "337 passed, 3 skipped" in log, version
report = json.loads((root / ".review/ef1-final.json").read_text(encoding="utf-8"))
assert not report["errors"] and report["gate"]["residual_attacks_observed"]
assert not report["cross_file_regression"]["errors"]
comparison = json.loads((root / ".review/ef1-comparison.json").read_text(encoding="utf-8"))
assert comparison["gate"]["status"] == "passed" and not comparison["gate"]["regressions"]
pi = json.loads((root / ".review/ef1-pi-final.json").read_text(encoding="utf-8"))
assert pi["external_attacker"]["mode"] == "mock" and not pi["errors"]
assert len(json.loads((root / ".review/ef1-pi-responses.json").read_text(encoding="utf-8"))["responses"]) == 39
changes = {"task": "EF1", "date": "2026-09-07", "kind": "explicit_edit_inventory_not_git_diff",
           "files": [{"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in files],
           "product_source_changed": False, "pi_main_changed": False,
           "note": "Run evidence and this QA script are local .review artifacts; the D5 bundle was not rebuilt."}
with (root / ".review/ef1-change-manifest.json").open("x", encoding="utf-8") as stream:
    json.dump(changes, stream, indent=2)
print(json.dumps({"status": "passed", "tracked_files": len(files), "frozen_corpora_verified": True,
                  "python_full_suites": 3, "pi_mode": "mock"}))
