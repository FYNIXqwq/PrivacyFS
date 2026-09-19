"""Read-only verification plus D2 review manifest/diff generation."""
from pathlib import Path
import ast
import difflib
import hashlib
import json
import re
import zipfile

import yaml

root = Path(__file__).resolve().parents[1]
baseline = json.loads((root / ".review/d2-baseline/files.json").read_text(encoding="utf-8"))
files = [p for folder in ("src", "tests", "evals", "benchmarks", ".github", "examples/d2")
         for p in (root / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
files += list(root.glob("*.md")) + [root / "pyproject.toml", root / "requirements-dev.lock"]
current = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
changed = sorted(name for name in baseline if name in current and baseline[name] != current[name])
added = sorted(current.keys() - baseline.keys())
deleted = sorted(baseline.keys() - current.keys())
assert not deleted, deleted
for p in files:
    if p.suffix == ".py":
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
for name in ("RELATIONS_D2.md", "D2_IMPLEMENTATION_REPORT.md", "AGENTS.md", "DEVELOPMENT_PLAN.md"):
    text = (root / name).read_text(encoding="utf-8")
    assert text.count("```") % 2 == 0, name
for name in (".github/workflows/tests.yml", "rules.example.yaml"):
    yaml.safe_load((root / name).read_text(encoding="utf-8"))
plan = (root / "DEVELOPMENT_PLAN.md").read_text(encoding="utf-8")
assert len(set(re.findall(r"D[0-7]-\d{2}", plan))) == 51
assert len(re.findall(r"\| D2-\d{2} \| DONE \|", plan)) == 7
for version in (310, 311, 312):
    data = (root / f".review/d2-tests-py{version}.txt").read_bytes()
    text = data.decode("utf-16") if data.startswith(b"\xff\xfe") else data.decode("utf-8")
    assert "224 passed, 3 skipped" in text
manifest = json.loads((root / "evals/data/d2-v1.manifest.json").read_text(encoding="utf-8"))
assert current["evals/data/d2-v1.jsonl"] == manifest["sha256"]
diff = []
with zipfile.ZipFile(root / ".review/d2-baseline/source.zip") as archived:
    for name in changed + added:
        if name.endswith(".jsonl"):
            continue
        before = archived.read(name).decode("utf-8").splitlines(keepends=True) if name in baseline else []
        after = (root / name).read_text(encoding="utf-8").splitlines(keepends=True)
        diff.extend(difflib.unified_diff(before, after, fromfile="before/" + name, tofile="after/" + name))
(root / ".review/d2-diff.patch").write_text("".join(diff), encoding="utf-8")
result = {"changed": changed, "added": added, "deleted": deleted,
          "after_sha256": {name: current[name] for name in changed + added}}
(root / ".review/d2-change-manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({"changed": changed, "added": added, "checks": "passed"}, indent=2))
