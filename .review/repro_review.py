"""Read-only project review: synthetic fixtures only; no project source edits."""
from pathlib import Path
import json
import tempfile
import zipfile
from unittest.mock import patch

from typer.testing import CliRunner
from privacyfs.cli import app, _prepare_rules
from privacyfs.config import Rules
from privacyfs.core import iter_mapped, sanitize_path, _detectors_for
from privacyfs.detectors.keywords import Finding, KeywordDetector
from privacyfs.detectors.llm import LLMDetector
from privacyfs.detectors.pinyin import PinyinDetector
from privacyfs.emit.mirror import build_mirror, MARKER
from privacyfs.emit.scrub import scrub_copy, EPOCH
from privacyfs.pseudonym import Pseudonymizer
from privacyfs import core

BASE = Path(tempfile.mkdtemp(prefix="evidence-", dir=Path(__file__).parent)).resolve()
print("FIXTURES", BASE)

def report(name, **values):
    print(json.dumps({"case": name, **values}, ensure_ascii=False))

def simple_rules(**kwargs):
    return Rules(use_ner=False, detect_pinyin=False, detect_en_names=False, **kwargs)

# A source file named like the mirror marker must not be overwritten via a hardlink.
root = BASE / "marker-source"
root.mkdir()
(root / MARKER).write_text("ORIGINAL-MARKER-CONTENT", encoding="utf-8")
p = Pseudonymizer(BASE / "marker.db")
mapped = iter_mapped(root, simple_rules(), p, with_size=False)
stats = build_mirror(root, mapped, BASE / "marker-view")
report("marker-source-overwritten", linked=stats.linked,
       source_preserved=(root / MARKER).read_text(encoding="utf-8") == "ORIGINAL-MARKER-CONTENT")
p.close()

# Literal alias-looking directory and pseudonymized directory collide.
root = BASE / "collision-source"
(root / "张三").mkdir(parents=True)
(root / "PERSON_0001").mkdir()
(root / "张三" / "a.txt").write_text("A")
(root / "PERSON_0001" / "b.txt").write_text("B")
p = Pseudonymizer(BASE / "collision.db")
p.alias("NAME_LIST", "张三")
mapped = iter_mapped(root, simple_rules(literal_names=["张三"]), p, with_size=False)
stats = build_mirror(root, mapped, BASE / "collision-view", force_copy=True)
report("alias-directory-collision", paths=[e.clean for e in mapped],
       directory_count=len([e for e in mapped if e.is_dir]),
       actual_directory_count=len([x for x in (BASE / "collision-view").iterdir() if x.is_dir()]),
       reported_collisions=stats.collisions)
p.close()

# Overlapping findings can alter an alias which was already generated.
p = Pseudonymizer(BASE / "replacement.db")
clean, aliases = sanitize_path(Path("Alice-SON.txt"),
    [Finding("PERSON", "Alice"), Finding("NAME_LIST", "SON")], p)
report("replacement-rewrites-alias", clean=clean, aliases=aliases)
p.close()

# A custom term with Unicode case equivalence triggers an unhandled lookup error.
try:
    result = KeywordDetector([("K", "NAME_LIST")]).find("K.txt")
    report("unicode-case-keyword", result=str(result))
except Exception as exc:
    report("unicode-case-keyword", error=type(exc).__name__, detail=str(exc))
try:
    result = KeywordDetector([("i", "NAME_LIST")]).find("İ.txt")
    report("unicode-case-keyword-i", result=str(result))
except Exception as exc:
    report("unicode-case-keyword-i", error=type(exc).__name__, detail=str(exc))

# Root-label context participates in the visible release but not the audit.
root = BASE / "北京-2024-06-17"
root.mkdir()
(root / "住院记录.txt").write_text("x")
runner = CliRunner()
result = runner.invoke(app, ["scan", str(root), "--ner", "off", "--no-pinyin",
    "--context-mode", "enforce", "-f", "json", "--db", str(BASE / "root.db")])
report("root-context", exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

# Explicit NER selection is overwritten by the command's implicit auto default.
rules_file = BASE / "ner.yaml"
rules_file.write_text("ner_engine: 'off'\n", encoding="utf-8")
prepared = _prepare_rules(rules_file, False, False, False, False, False, "auto")
report("rules-ner-overridden", configured="off", actual=prepared.ner_engine)

# XML namespace prefixes are arbitrary: valid alternative prefixes retain authors.
src = BASE / "namespace.docx"
dst = BASE / "namespace-clean.docx"
with zipfile.ZipFile(src, "w") as z:
    z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:d="http://purl.org/dc/elements/1.1/"><d:creator>SECRET_AUTHOR</d:creator></cp:coreProperties>')
scrubbed = scrub_copy(src, dst)
with zipfile.ZipFile(dst) as z:
    report("office-namespace-prefix", reported_scrubbed=scrubbed,
           author_survives=b"SECRET_AUTHOR" in z.read("docProps/core.xml"))

# Fresh cache clients with different models share old NONE results.
p = Pseudonymizer(BASE / "llm-cache.db")
p.llm_cache_put({"nickname.txt": "NONE"})
det = LLMDetector(model="new-model", cache=p)
with patch.object(det, "_chat", return_value="1:PERSON") as chat:
    found = det.find_batch(["nickname.txt"])
    report("llm-model-cache", model_called=chat.called, detections=len(found))
p.close()

# LLM is never asked about unknown portions of a partly-detected filename.
p = Pseudonymizer(BASE / "partial-llm.db")
with patch.object(LLMDetector, "_chat", return_value="1:PERSON") as chat:
    result = core._component_findings(["青禾计划-工资.txt"],
        simple_rules(use_llm=True), p, 1, None, None)
    report("llm-partial-name", llm_called=chat.called,
           findings=[f.surface for f, _ in result["青禾计划-工资.txt"]])
p.close()

# ProcessPoolExecutor.shutdown clears its private process map.
class FakePool:
    _processes = {}
    def shutdown(self, **kwargs):
        self._processes = None
core._register_pool(FakePool())
try:
    core.kill_active_pool()
    report("pool-cancel", completed=True)
except Exception as exc:
    report("pool-cancel", error=type(exc).__name__, detail=str(exc))

report("pinyin-samples", detections={s: [f.surface for f in PinyinDetector().find(s)]
       for s in ["main.go", "config.ini", "README.md", "ZhangJinan.txt", "zhangjinan.txt", "ZhangSan.txt"]})

# Even built-in terms can hit re.IGNORECASE/lower discrepancies.
try:
    list(core._component_findings(["VİSA.pdf"], simple_rules(), None, 1, None, None))
    report("unicode-default-terms", completed=True)
except Exception as exc:
    report("unicode-default-terms", error=type(exc).__name__, detail=str(exc))

# Repeatedly scanning the same filename under different detector categories
# allocates two distinct pseudonyms despite the same prefix and surface.
p = Pseudonymizer(BASE / "category.db")
report("category-alias-consistency", ner=p.alias("PERSON", "张三"),
       explicit=p.alias("NAME_LIST", "张三"))
p.close()

# The scope of explicit names includes organizations, but the graph treats
# all NAME_LIST values as evidence of the same person.
from privacyfs.context import build_privacy_graph
from privacyfs.core import detect_entries, walk_entries
from privacyfs.detectors.context_terms import ContextTermDetector
root = BASE / "subjects"
for subject in ("a", "b", "c"):
    folder = root / subject
    folder.mkdir(parents=True)
    (folder / "某某公司-记录.txt").write_text("x")
rules = simple_rules(literal_names=["某某公司"])
raws, sizes = walk_entries(root, rules, with_size=False)
entries = detect_entries(raws, rules)
report("organization-as-person", top_level_directories=3,
       inferred_subjects=len(build_privacy_graph(entries).subjects))
report("context-script-variants", findings={s: [f.category for f in ContextTermDetector().find(s)]
       for s in ["计算机专业", "計算機專業", "医疗-诊断", "醫療-診斷"]})

# Scrubbed marker statistics and timestamp are written after final freezing.
root = BASE / "scrub-source"
root.mkdir()
(root / "normal.txt").write_text("plain")
p = Pseudonymizer(BASE / "scrub-stats.db")
mapped = iter_mapped(root, simple_rules(), p, with_size=False)
dest = BASE / "scrub-view"
stats = build_mirror(root, mapped, dest, scrub_metadata=True)
marker = json.loads((dest / MARKER).read_text(encoding="utf-8"))
report("scrub-marker", actual_files=1, marker_files=marker["files"],
       root_timestamp_frozen=dest.stat().st_mtime == EPOCH,
       marker_timestamp_frozen=(dest / MARKER).stat().st_mtime == EPOCH)
p.close()

# The LLM cache query builds one SQL parameter per unique name before applying max_items.
p = Pseudonymizer(BASE / "query-limit.db")
try:
    p.llm_cache_get([f"name-{i}" for i in range(250001)])
    report("llm-cache-query-limit", names=250001, completed=True)
except Exception as exc:
    report("llm-cache-query-limit", names=250001, error=type(exc).__name__, detail=str(exc))
p.close()
