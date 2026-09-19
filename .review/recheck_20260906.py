"""Additional synthetic review cases; writes only fresh review fixtures."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from typer.testing import CliRunner
from privacyfs.cli import app
from privacyfs.config import Rules
from privacyfs.core import iter_mapped
from privacyfs.detectors.llm import LLMDetector
from privacyfs.emit.scrub import scrub_copy

base = Path(tempfile.mkdtemp(prefix="recheck-20260906-", dir=Path(__file__).parent)).resolve()
results = []


def record(case, **values):
    results.append({"case": case, **values})


# Page-by-page rewriting may drop document navigation despite scrub success.
source = base / "bookmarked.pdf"
target = base / "bookmarked-clean.pdf"
writer = PdfWriter()
writer.add_blank_page(width=100, height=100)
writer.add_outline_item("Chapter One", 0)
with source.open("wb") as stream:
    writer.write(stream)
success = scrub_copy(source, target)
record("pdf_outline_preservation", scrub_success=success,
       source_outlines=len(PdfReader(source).outline),
       target_outlines=len(PdfReader(target).outline))


# A real copy failure should be distinguishable from a complete mirror.
root = base / "source"
root.mkdir()
(root / "normal.txt").write_text("synthetic payload", encoding="utf-8")
runner = CliRunner()
with patch("privacyfs.emit.mirror.shutil.copy2", side_effect=OSError("synthetic copy failure")):
    result = runner.invoke(app, ["mirror", str(root), str(base / "incomplete-view"),
        "--copy", "--ner", "off", "--no-pinyin", "--db", str(base / "mirror.db")])
record("mirror_copy_failure_status", exit_code=result.exit_code,
       stderr=result.stderr, file_exists=(base / "incomplete-view" / "normal.txt").exists())


# Verify YAML-only NER selection at the actual CLI pipeline boundary.
rules_file = base / "ner.yaml"
rules_file.write_text("ner_engine: 'off'\n", encoding="utf-8")
seen = []


def observe_rules(*args, **kwargs):
    seen.append(args[1].ner_engine)
    return iter_mapped(*args, **kwargs)


with patch("privacyfs.cli.iter_mapped", side_effect=observe_rules):
    result = runner.invoke(app, ["scan", str(root), "--rules", str(rules_file),
        "--no-pinyin", "--no-size", "-f", "json", "--db", str(base / "ner.db")])
record("ner_yaml_actual_cli", exit_code=result.exit_code, configured="off", effective=seen)


# Inspect network target without making any network request.
requests = []


def capture_request(request, **kwargs):
    requests.append({"url": request.full_url,
                     "contains_filename": "synthetic-private-name.txt" in request.data.decode()})
    raise OSError("synthetic network stub; no request sent")


detector = LLMDetector(url="https://example.invalid", max_items=1)
with patch("privacyfs.detectors.llm.urllib.request.urlopen", side_effect=capture_request):
    detector.find_batch(["synthetic-private-name.txt"])
record("remote_url_accepted_no_network_sent", requests=requests)

payload = {"fixtures": str(base), "results": results}
(base / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False, indent=2))
