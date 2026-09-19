"""Final D4 evidence checks and change manifest, without Git initialization."""
from pathlib import Path
import ast
import difflib
import hashlib
import json
import re
import zipfile

import yaml

root = Path(__file__).resolve().parents[1]
baseline = json.loads((root / ".review/d4-baseline/files.json").read_text(encoding="utf-8"))
files = [p for folder in ("src", "tests", "evals", "benchmarks", ".github", "examples")
         for p in (root / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
files += list(root.glob("*.md")) + [root / "pyproject.toml", root / "requirements-dev.lock"]
current = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
changed = sorted(name for name in baseline if name in current and baseline[name] != current[name])
added = sorted(current.keys() - baseline.keys())
deleted = sorted(baseline.keys() - current.keys())
assert not deleted
for path in files:
    if path.suffix == ".py": ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if path.suffix == ".json": json.loads(path.read_text(encoding="utf-8"))
for name in ("WORKSPACES_D4.md", "D4_IMPLEMENTATION_REPORT.md", "AGENTS.md", "DEVELOPMENT_PLAN.md", "RELEASES_D3.md"):
    assert (root / name).read_text(encoding="utf-8").count("```") % 2 == 0, name
for name in (".github/workflows/tests.yml", "rules.example.yaml"):
    yaml.safe_load((root / name).read_text(encoding="utf-8"))
plan = (root / "DEVELOPMENT_PLAN.md").read_text(encoding="utf-8")
assert len(set(re.findall(r"D[0-7]-\d{2}", plan))) == 51
assert len(re.findall(r"\| D4-\d{2} \| DONE \|", plan)) == 7
for version in (310, 311, 312):
    data = (root / f".review/d4-tests-py{version}.txt").read_bytes()
    text = data.decode("utf-16") if data.startswith(b"\xff\xfe") else data.decode("utf-8")
    assert "303 passed, 3 skipped" in text
evaluation = json.loads((root / ".review/d4-evaluation-final.json").read_text())
assert evaluation["errors"] == [] and evaluation["counts"]["refreshes"] == 320
assert current["evals/data/d2-v1.jsonl"] == evaluation["sha256"]
benchmark = json.loads((root / ".review/d4-benchmark-final.json").read_text())
assert benchmark["phases"]["warm"]["graph_cache_hits"] == 40
assert benchmark["phases"]["add_unrelated"]["rebuilt_components"] == 1
demo = json.loads((root / ".review/d4-demo-results.json").read_text())
assert any(e.get("state") == "PUBLISHED" for e in demo)
assert demo[-1]["publication_preserved"] and not demo[-1]["workspace_exists"]
for phase in range(4):
    name = f"D{phase}_IMPLEMENTATION_REPORT.md"
    assert baseline[name] == current[name]
diff = []
with zipfile.ZipFile(root / ".review/d4-baseline/source.zip") as archived:
    for name in changed + added:
        if name.endswith(".jsonl"): continue
        before = archived.read(name).decode("utf-8").splitlines(keepends=True) if name in baseline else []
        after = (root / name).read_text(encoding="utf-8").splitlines(keepends=True)
        diff.extend(difflib.unified_diff(before, after, fromfile="before/" + name, tofile="after/" + name))
(root / ".review/d4-diff.patch").write_text("".join(diff), encoding="utf-8")
(root / ".review/d4-change-manifest.json").write_text(json.dumps({"changed": changed, "added": added,
    "deleted": deleted, "after_sha256": {name: current[name] for name in changed + added}}, indent=2), encoding="utf-8")
print(json.dumps({"changed": changed, "added": added, "checks": "passed"}, indent=2))
