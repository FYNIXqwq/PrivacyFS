"""The trial must actually publish selected complex input as fresh CSV."""
import csv
import io
import json
import threading
import zipfile

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.diagnostics import doctor, write_diagnostic_bundle
from privacyfs.documents import DocumentSession, Limits
from privacyfs.documents.models import ProcessingStopped
from privacyfs.sample_documents import create_trial, docx_bytes
from privacyfs.state_store import create_workspace
from privacyfs.review import refresh_workspace, prepare_workspace, local_workspace_details
from privacyfs.release import review_release, export_release


def test_trial_docx_pdf_to_csv_preserves_task_and_omits_unprocessed_content(tmp_path):
    trial = tmp_path / "trial"
    create_trial(trial)
    source, state, output = trial / "source", trial / "private", trial / "output"
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    workspace = create_workspace(source, source / "relations.json", source / "profile.json", state)["workspace_id"]
    report = refresh_workspace(workspace, state)
    local = local_workspace_details(workspace, state, expected_revision=report["revision"])
    assert any(o["locator"]["table"] == 0 for o in local["occurrences"])
    assert any(o["locator"]["page"] == 1 for o in local["occurrences"])
    warm = refresh_workspace(workspace, state)
    assert warm["metrics"]["parse_cache_hits"] == 3
    plan = prepare_workspace(workspace, state)
    assert any(p["format"] == "docx" and p["parsing_status"] == "partial" for p in plan["plan"]["input_projections"])
    review_release(plan["plan_id"], state, approve=plan["approval_digest"])
    published = export_release(plan["plan_id"], output, state)
    files = list((output / published["release_id"]).iterdir())
    assert len(files) == 4 and all(p.suffix in {".csv", ".json"} for p in files)
    combined = b"".join(p.read_bytes() for p in files)
    for value in ("合成示例企业甲", "SYNTHETIC_PRIVATE_HEADER", "SYNTHETIC_PRIVATE_AUTHOR", "SYNTHETIC_PRIVATE_ATTACHMENT"):
        assert value.encode() not in combined
    status = next(list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8")))) for p in files if p.suffix == ".csv" and "status" in p.read_text(encoding="utf-8"))
    assert [r["status"] for r in status] == ["paused", "ended"]
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}


def test_doctor_bundle_has_no_paths_customer_files_or_state_secrets(tmp_path):
    report = doctor(self_test=True)
    assert report["self_test"] == "passed"
    target = tmp_path / "diagnostics.zip"
    write_diagnostic_bundle(target, report)
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {"diagnostics.json", "README.txt"}
        payload = archive.read("diagnostics.json").decode()
        assert "source_path" not in payload and str(tmp_path) not in payload
        assert json.loads(payload)["model_calls_enabled"] is False
    with pytest.raises(FileExistsError):
        write_diagnostic_bundle(target, report)


def test_container_worker_running_cancel_and_memory_guard(tmp_path, monkeypatch):
    import privacyfs.documents.containers as containers
    cancel = threading.Event()
    original = containers.WindowsJob if __import__('os').name == 'nt' else None
    if original:
        class CancelAfterAssignment(original):
            def __init__(self, *args):
                super().__init__(*args)
                cancel.set()
        monkeypatch.setattr(containers, "WindowsJob", CancelAfterAssignment)
    else:
        cancel.set()
    (tmp_path / "a.docx").write_bytes(docx_bytes(paragraphs=("id=A;status=ready",)))
    with DocumentSession(tmp_path, cancel=cancel) as session:
        doc = session.parse(session.capture("a.docx"))
        assert doc.coverage.reason == "CANCELLED"


def test_cli_sample_does_not_overwrite_and_inspect_reports_partial(tmp_path):
    runner = CliRunner()
    folder = tmp_path / "sample"
    made = runner.invoke(app, ["sample", str(folder)])
    assert made.exit_code == 0
    assert runner.invoke(app, ["sample", str(folder)]).exit_code == 4
    inspected = runner.invoke(app, ["inspect", str(folder / "source" / "clients.docx")])
    assert inspected.exit_code == 4
    report = json.loads(inspected.stdout)["documents"][0]
    assert report["status"] == "partial" and report["finding_count"] is None
    assert "headers" in report["unprocessed"]


def test_running_worker_timeout_kills_child(tmp_path, monkeypatch):
    import subprocess, sys, time
    import privacyfs.documents.containers as containers
    original = subprocess.Popen
    children = []
    def hanging_worker(args, **kwargs):
        child = original([getattr(sys, '_base_executable', sys.executable), '-S', '-c',
            "import sys,time;sys.stdin.buffer.read();time.sleep(60)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(containers.subprocess, 'Popen', hanging_worker)
    (tmp_path / 'a.docx').write_bytes(docx_bytes(paragraphs=('id=A;status=ready',)))
    start = time.monotonic()
    with DocumentSession(tmp_path, limits=Limits(max_seconds=0.5)) as session:
        doc = session.parse(session.capture('a.docx'))
        assert doc.coverage.reason == 'TIME_LIMIT'
    assert time.monotonic() - start < 5
    assert children and all(p.poll() is not None for p in children)
