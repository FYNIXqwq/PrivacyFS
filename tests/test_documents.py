"""D1 document contracts: synthetic sources, no model/network calls."""
import csv
import io
import json
from pathlib import Path
import threading

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.config import Rules
from privacyfs.documents import DocumentSession, Limits, ParseStatus, inspect_document
from privacyfs.documents.models import StaleLocatorError, ClosedSessionError
from privacyfs.normalization import normalize_with_map
from privacyfs.source_map import SpanEdit, apply_edits


def test_snapshot_parse_uses_captured_bytes_and_rejects_new_revision_locator(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("姓名：张三", encoding="utf-8")
    with DocumentSession(tmp_path) as session:
        old = session.capture("a.txt")
        source.write_text("姓名：李四", encoding="utf-8")
        old_doc = session.parse(old)
        assert old_doc.text == "姓名：张三"
        findings = inspect_document(old_doc, Rules(literal_names=["张三"]))
        location = findings[0].locator
        assert old_doc.resolve(location) == "张三"
        new_doc = session.parse(session.capture("a.txt"))
        assert new_doc.text == "姓名：李四"
        assert new_doc.snapshot.document_id == old_doc.snapshot.document_id
        assert new_doc.snapshot.revision_id != old_doc.snapshot.revision_id
        with pytest.raises(StaleLocatorError):
            new_doc.resolve(location)


@pytest.mark.parametrize("encoding,bom", [
    ("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"),
    ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
    ("utf-32-le", b"\xff\xfe\x00\x00"), ("utf-32-be", b"\x00\x00\xfe\xff"),
])
def test_bom_and_newline_offsets_are_explicit(tmp_path, encoding, bom):
    text = "甲\r\n张三🙂\n終"
    (tmp_path / "a.txt").write_bytes(bom + text.encode(encoding))
    with DocumentSession(tmp_path) as session:
        doc = session.parse(session.capture("a.txt"))
        assert doc.coverage.status is ParseStatus.COMPLETE
        assert doc.text == text
        hit = inspect_document(doc, Rules(literal_names=["张三"]))[0]
        assert (hit.locator.start, hit.locator.end, hit.locator.line) == (3, 5, 2)
        assert doc.resolve(hit.locator) == "张三"
        assert doc.encoding == encoding


@pytest.mark.parametrize("raw,needle,expected", [
    ("前ＡＢＣ後", "abc", "ＡＢＣ"),
    ("xﬃy", "ffi", "ﬃ"),
    ("xStraße!", "strasse", "Straße"),
    ("xCafe\u0301!", "café", "Cafe\u0301"),
    ("前計算機專業後", "计算机专业", "計算機專業"),
    ("x\u1100\u1161y", "가", "\u1100\u1161"),
])
def test_normalization_preserves_original_cover(raw, needle, expected):
    normalized = normalize_with_map(raw)
    at = normalized.text.index(needle)
    start, end = normalized.source_span(at, at + len(needle))
    assert raw[start:end] == expected


def test_expansion_subspan_maps_to_whole_source_grapheme():
    normalized = normalize_with_map("ﬃ")
    assert normalized.source_span(1, 2) == (0, 1)


def test_csv_cells_preserve_quotes_newlines_empty_values_and_leading_zeroes(tmp_path):
    raw = 'id,note,amount\r\n001,"张三说""你好""\r\n第二行",0012\r\n002,,=1+1\r\n'
    (tmp_path / "a.csv").write_bytes(raw.encode("utf-8"))
    with DocumentSession(tmp_path) as session:
        doc = session.parse(session.capture("a.csv"))
        assert doc.coverage.status is ParseStatus.COMPLETE
        expected = list(csv.reader(io.StringIO(raw, newline=""), strict=True))
        assert doc.csv_rows == expected
        hit = inspect_document(doc, Rules(literal_names=["张三"]))[0]
        assert (hit.locator.row, hit.locator.column) == (1, 1)
        assert doc.resolve(hit.locator) == "张三"
        quote_block = next(block for block in doc.blocks if block.text.startswith("张三说"))
        start, end = quote_block.source_span(3, 4)
        assert doc.text[start:end] == '""'
        out = io.StringIO(newline="")
        csv.writer(out, lineterminator=doc.newline).writerows(doc.csv_rows)
        assert list(csv.reader(io.StringIO(out.getvalue(), newline=""), strict=True)) == expected
        assert source_bytes(tmp_path / "a.csv") == raw.encode("utf-8")


def source_bytes(path):
    return path.read_bytes()


def test_csv_never_joins_findings_across_columns(tmp_path):
    (tmp_path / "a.csv").write_text("left,right\n张,三\n", encoding="utf-8")
    with DocumentSession(tmp_path) as session:
        doc = session.parse(session.capture("a.csv"))
        assert not inspect_document(doc, Rules(literal_names=["张三"]))


@pytest.mark.parametrize("raw", ['a,b\n"unfinished', 'a,b\n"closed"junk,x'])
def test_malformed_csv_is_not_a_complete_negative(tmp_path, raw):
    (tmp_path / "a.csv").write_text(raw, encoding="utf-8")
    with DocumentSession(tmp_path) as session:
        doc = session.parse(session.capture("a.csv"))
        assert doc.coverage.status is ParseStatus.FAILED
        assert doc.coverage.reason == "INVALID_CSV"


def test_decoding_failure_and_explicit_gb18030(tmp_path):
    (tmp_path / "a.txt").write_bytes("张三".encode("gb18030"))
    with DocumentSession(tmp_path) as session:
        snapshot = session.capture("a.txt")
        assert session.parse(snapshot).coverage.reason == "DECODING_FAILED"
        assert session.parse(snapshot, encoding="gb18030").text == "张三"


def test_limits_and_cancellation_leave_no_disk_snapshots(tmp_path):
    (tmp_path / "a.txt").write_text("x" * 40)
    before = list(tmp_path.iterdir())
    with DocumentSession(tmp_path, limits=Limits(max_file_bytes=8)) as session:
        doc = session.parse(session.capture("a.txt"))
        assert doc.coverage.status is ParseStatus.FAILED
        assert doc.coverage.reason == "FILE_LIMIT"
        assert session.bytes_held == 0
    cancel = threading.Event()
    cancel.set()
    with DocumentSession(tmp_path, cancel=cancel) as session:
        doc = session.parse(session.capture("a.txt"))
        assert doc.coverage.reason == "CANCELLED"
    assert list(tmp_path.iterdir()) == before


def test_partial_csv_coverage_is_explicit(tmp_path):
    (tmp_path / "a.csv").write_text("a,b\nx,y\nz,w\n")
    with DocumentSession(tmp_path, limits=Limits(max_csv_rows=2)) as session:
        doc = session.parse(session.capture("a.csv"))
        assert doc.coverage.status is ParseStatus.PARTIAL
        assert doc.coverage.parsed_characters < len(doc.text)
        assert len(doc.csv_rows) == 2


def test_session_close_invalidates_managed_document_handles(tmp_path):
    (tmp_path / "a.txt").write_text("PRIVATE", encoding="utf-8")
    session = DocumentSession(tmp_path)
    snapshot = session.capture("a.txt")
    doc = session.parse(snapshot)
    block = doc.blocks[0]
    session.close()
    assert session.bytes_held == 0
    with pytest.raises(ClosedSessionError):
        _ = doc.text
    with pytest.raises(ClosedSessionError):
        _ = block.text
    with pytest.raises(ClosedSessionError):
        session.parse(snapshot)


def test_public_inspection_has_no_raw_values_paths_or_digest(tmp_path):
    from privacyfs.documents import public_report
    (tmp_path / "SECRET_NAME.txt").write_text("姓名：张三", encoding="utf-8")
    with DocumentSession(tmp_path) as session:
        doc = session.parse(session.capture("SECRET_NAME.txt"))
        findings = inspect_document(doc, Rules(literal_names=["张三"]))
        encoded = json.dumps(public_report(doc, findings), ensure_ascii=False)
        for secret in ("张三", "SECRET_NAME", str(tmp_path), doc.snapshot.content_digest):
            assert secret not in encoded
        assert "张三" not in repr(doc) and "张三" not in repr(findings)


def test_replacement_has_deterministic_overlap_resolution_and_no_cascade():
    edits = [SpanEdit(0, 5, "PERSON_1"), SpanEdit(6, 9, "PERSON_2")]
    assert apply_edits("Alice-SON", edits) == "PERSON_1-PERSON_2"
    assert apply_edits("AliceSmith", [SpanEdit(0, 5, "short"), SpanEdit(0, 10, "long")]) == "long"
    with pytest.raises(ValueError):
        apply_edits("short", [SpanEdit(0, 30, "invalid")])


def test_inspect_cli_reports_failure_and_preserves_legacy_scan(tmp_path):
    (tmp_path / "bad.csv").write_text('"unfinished')
    result = CliRunner().invoke(app, ["inspect", str(tmp_path / "bad.csv")])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["documents"][0]["status"] == "failed"
    assert "unfinished" not in result.output and "bad.csv" not in result.output
    result = CliRunner().invoke(app, ["scan", str(tmp_path), "--ner", "off",
        "--db", str(tmp_path / "mapping.db"), "-f", "json", "--no-size"])
    assert result.exit_code == 0
    assert "documents" not in json.loads(result.stdout)


def test_path_signal_adapter_does_not_read_file_contents(tmp_path):
    from privacyfs.models import DetectedEntry, DetectedSignal
    entry = DetectedEntry("ENTRY_test", Path("张三/档案.txt"), signals=[
        DetectedSignal("PERSON", "张三", "张三", 1.0, "test", 0)])
    with DocumentSession(tmp_path) as session:
        doc, findings = session.adapt_path_entry(entry)
        assert doc.coverage.scope == "filename_only"
        assert doc.resolve(findings[0].locator) == "张三"
        assert findings[0].locator.component_index == 0
        assert not (tmp_path / "张三").exists()


def test_total_memory_budget_can_be_released(tmp_path):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"abcdef")
    with DocumentSession(tmp_path, limits=Limits(max_total_bytes=8)) as session:
        first = session.capture("a.txt")
        doc = session.parse(first)
        failed = session.capture("b.txt")
        assert failed.capture_reason == "TOTAL_BYTE_LIMIT"
        assert session.bytes_held == 6
        session.release(first)
        with pytest.raises(ClosedSessionError):
            _ = doc.text
        assert session.capture("b.txt").capture_reason is None


def test_snapshot_rejects_source_changes_during_read(tmp_path, monkeypatch):
    import privacyfs.documents.session as session_module
    (tmp_path / "a.txt").write_bytes(b"first")
    original = session_module.os.fdopen
    class ChangingReader:
        def __init__(self, stream):
            self.stream, self.changed = stream, False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, count):
            value = self.stream.read(count)
            if not self.changed:
                self.changed = True
                with (tmp_path / "a.txt").open("ab") as changed:
                    changed.write(b"changed")
            return value
    monkeypatch.setattr(session_module.os, "fdopen", lambda *a, **kw: ChangingReader(original(*a, **kw)))
    with DocumentSession(tmp_path) as session:
        snapshot = session.capture("a.txt")
        assert snapshot.capture_reason == "SOURCE_CHANGED"
        assert session.bytes_held == 0


def test_adversarial_regex_is_bounded(tmp_path):
    import time
    from privacyfs.documents.models import ProcessingStopped
    (tmp_path / "a.txt").write_text("a" * 20_000 + "!")
    rules = Rules(keywords=[], literal_names=[], professions=[], identity_terms=[], regexes=[r"(a+)+$"])
    with DocumentSession(tmp_path) as session:
        doc = session.parse(session.capture("a.txt"))
        started = time.monotonic()
        with pytest.raises(ProcessingStopped, match="TIME_LIMIT"):
            inspect_document(doc, rules, limits=Limits(max_seconds=0.02))
        assert time.monotonic() - started < 2


def test_capture_refuses_parent_escape_without_reading(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("private")
    with DocumentSession(root) as session:
        snapshot = session.capture("../secret.txt")
        assert snapshot.capture_reason == "INVALID_PATH"
        assert session.bytes_held == 0


def test_inspection_inherits_session_limits_and_cancel(tmp_path):
    from privacyfs.documents.models import ProcessingStopped
    (tmp_path / "a.txt").write_text("张三 李四", encoding="utf-8")
    cancel = threading.Event()
    with DocumentSession(tmp_path, limits=Limits(max_mentions=1), cancel=cancel) as session:
        snapshot = session.capture("a.txt")
        doc = session.parse(snapshot)
        with pytest.raises(ProcessingStopped, match="FINDING_LIMIT"):
            inspect_document(doc, Rules(literal_names=["张三", "李四"]))
        cancel.set()
        assert session.parse(snapshot).coverage.reason == "CANCELLED"
        with pytest.raises(ProcessingStopped, match="CANCELLED"):
            inspect_document(doc, Rules(literal_names=["张三"]))


def test_csv_round_trip_generated_cases(tmp_path):
    import random
    rng = random.Random(9041)
    with DocumentSession(tmp_path) as session:
        for case in range(80):
            rows = [["".join(rng.choice('ab张三,"\r\n 01') for _ in range(rng.randrange(15)))
                     for _ in range(rng.randrange(1, 6))] for _ in range(rng.randrange(1, 7))]
            buffer = io.StringIO(newline="")
            csv.writer(buffer, lineterminator="\r\n").writerows(rows)
            (tmp_path / "random.csv").write_bytes(buffer.getvalue().encode("utf-8"))
            snapshot = session.capture("random.csv")
            doc = session.parse(snapshot)
            assert doc.coverage.status is ParseStatus.COMPLETE, case
            assert doc.csv_rows == rows
            for block in doc.blocks:
                for index, char in enumerate(block.text):
                    a, b = block.source_span(index, index + 1)
                    assert doc.text[a:b] in ({'"', '""'} if char == '"' else {char})
            session.release(snapshot)


def test_inspect_cli_uses_no_models_or_mapping_database(tmp_path, monkeypatch):
    (tmp_path / "note.txt").write_text("姓名：張三", encoding="utf-8")
    rules = tmp_path / "rules.yaml"
    rules.write_text("literal_names: [张三]\nuse_llm: true\n", encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("D1 must not call models or create the mapping DB")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("privacyfs.cli.Pseudonymizer", forbidden)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = CliRunner().invoke(app, ["inspect", str(tmp_path / "note.txt"), "--rules", str(rules)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["documents"][0]
    assert report["finding_count"] == 1 and report["privacy_verified"] is False
    assert "張三" not in result.output and "张三" not in result.output
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_real_entrypoint_routes_inspect_and_returns_valid_json(tmp_path):
    import subprocess
    import sys
    (tmp_path / "note.txt").write_text("ordinary text")
    result = subprocess.run([sys.executable, "-m", "privacyfs.cli", "inspect", str(tmp_path / "note.txt")],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == "d1-inspection-1"
    assert report["documents"][0]["status"] == "complete"


def test_explicit_organization_takes_precedence_after_normalization(tmp_path):
    (tmp_path / "a.txt").write_text("ＡＣＭＥ", encoding="utf-8")
    with DocumentSession(tmp_path) as session:
        document = session.parse(session.capture("a.txt"))
        found = inspect_document(document, Rules(literal_names=["Acme"], literal_orgs=["ACME"]))
        assert [f.category for f in found] == ["ORG"]


def test_failed_path_adapter_releases_its_snapshot(tmp_path):
    from privacyfs.models import DetectedEntry, DetectedSignal
    from privacyfs.documents.models import ProcessingStopped
    entry = DetectedEntry("ENTRY_test", Path("张三-张三.txt"), signals=[
        DetectedSignal("PERSON", "张三", "张三", 1.0, "test", 0)])
    with DocumentSession(tmp_path, limits=Limits(max_mentions=1)) as session:
        with pytest.raises(ProcessingStopped, match="FINDING_LIMIT"):
            session.adapt_path_entry(entry)
        assert session.bytes_held == 0


def test_capture_does_not_compare_ctime_between_stat_and_fstat(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import privacyfs.documents.session as session_module
    (tmp_path / "a.txt").write_text("stable content")
    original = session_module.os.fstat
    def different_ctime(descriptor):
        info = original(descriptor)
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns + 12345)
    monkeypatch.setattr(session_module.os, "fstat", different_ctime)
    with DocumentSession(tmp_path) as session:
        snapshot = session.capture("a.txt")
        assert snapshot.capture_reason is None
        assert session.parse(snapshot).text == "stable content"


def test_snapshot_refuses_windows_junction(tmp_path):
    import os
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows junction regression")
    root, private = tmp_path / "root", tmp_path / "private"
    root.mkdir()
    private.mkdir()
    (private / "secret.txt").write_text("must not read")
    linked = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(root / "link"), str(private)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert linked.returncode == 0, linked.stderr
    with DocumentSession(root) as session:
        snapshot = session.capture("link/secret.txt")
        assert snapshot.capture_reason == "REPARSE_POINT"
        assert session.bytes_held == 0
