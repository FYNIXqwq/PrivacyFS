"""Final D5 source/package checks and review evidence without Git."""
from pathlib import Path
import ast
import difflib
import hashlib
import json
import re
import zipfile

import yaml

root = Path(__file__).resolve().parents[1]
baseline = json.loads((root / ".review/d5-baseline/files.json").read_text(encoding="utf-8"))
files = [p for folder in ("src", "tests", "evals", "benchmarks", ".github", "examples", "packaging")
         for p in (root / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
files += list(root.glob("*.md")) + [root / "pyproject.toml", root / "requirements-dev.lock"]
current = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
changed = sorted(n for n in baseline if n in current and baseline[n] != current[n])
added = sorted(current.keys() - baseline.keys())
assert not baseline.keys() - current.keys()
for path in files:
    if path.suffix == ".py": ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if path.suffix == ".json": json.loads(path.read_text(encoding="utf-8"))
for name in ("TRIAL_D5.md", "FORMATS_D5.md", "PILOT_FEEDBACK_TEMPLATE.md", "D5_IMPLEMENTATION_REPORT.md", "AGENTS.md", "DEVELOPMENT_PLAN.md"):
    assert (root / name).read_text(encoding="utf-8").count("```") % 2 == 0, name
yaml.safe_load((root / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
plan = (root / "DEVELOPMENT_PLAN.md").read_text(encoding="utf-8")
assert len(set(re.findall(r"D[0-7]-\d{2}", plan))) == 51
assert len(re.findall(r"\| D5-0[1-6] \| DONE", plan)) == 6
assert "| D5-07 | TODO（用户延期）" in plan
for version in (310, 311, 312):
    raw = (root / f".review/d5-tests-py{version}.txt").read_bytes()
    text = raw.decode("utf-16") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8")
    assert "325 passed, 3 skipped" in text
    installed = json.loads((root / f".review/d5-install-validation-{version}.json").read_text())
    assert installed["state_and_publication_preserved"]
    assert any(e.get("state") == "PUBLISHED" for e in installed["events"])
evaluation = json.loads((root / ".review/d5-evaluation-final.json").read_text())
assert not evaluation["errors"] and evaluation["metrics"]["cases"] == 60
assert current["evals/data/d3-v1.jsonl"] == evaluation["sha256"]
bundle = root / ".review/PrivacyFS-0.2.0a1-win64"
manifest = json.loads((bundle / "manifest.json").read_text())
for name, digest in manifest["files"].items():
    assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == digest, name
wheel = bundle / "wheelhouse/privacyfs-0.2.0a1-py3-none-any.whl"
with zipfile.ZipFile(wheel) as archive:
    for path in (root / "src/privacyfs").rglob("*.py"):
        if "__pycache__" not in path.parts:
            assert archive.read("privacyfs/" + path.relative_to(root / "src/privacyfs").as_posix()) == path.read_bytes(), path
    assert all(not n.endswith((".db", ".gguf")) for n in archive.namelist())
assert (bundle / "manage.py").read_bytes() == (root / "packaging/manage.py").read_bytes()
archive_path = root / ".review/PrivacyFS-0.2.0a1-win64.zip"
assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == "764e1fd1e07d856525118ec240253cfb1e3c645984334d77169ac97d20f09624"
for phase in range(5):
    name = f"D{phase}_IMPLEMENTATION_REPORT.md"
    assert baseline[name] == current[name]
diff = []
with zipfile.ZipFile(root / ".review/d5-baseline/source.zip") as archived:
    for name in changed + added:
        if name.endswith(".jsonl"): continue
        before = archived.read(name).decode("utf-8").splitlines(keepends=True) if name in baseline else []
        after = (root / name).read_text(encoding="utf-8").splitlines(keepends=True)
        diff.extend(difflib.unified_diff(before, after, fromfile="before/" + name, tofile="after/" + name))
(root / ".review/d5-diff.patch").write_text("".join(diff), encoding="utf-8")
(root / ".review/d5-change-manifest.json").write_text(json.dumps({"changed": changed, "added": added, "deleted": [],
    "after_sha256": {n: current[n] for n in changed + added}}, indent=2), encoding="utf-8")
print(json.dumps({"changed": changed, "added": added, "checks": "passed"}, indent=2))
