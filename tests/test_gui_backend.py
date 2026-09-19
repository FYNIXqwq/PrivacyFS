"""Local GUI workflow invariants, synthetic files only; no real model calls."""
import json
from importlib.util import find_spec
from pathlib import Path
import threading
import time

import pytest

from privacyfs.gui.backend import ScanOptions, scan, export_report, ScanController


def _stalled_worker(options, cancel, events):
    events.put({"kind":"progress","stats":{"visited":1}})
    time.sleep(15)


def fixture(root):
    (root / "张三").mkdir()
    (root / "张三" / "notes.txt").write_text("private body must not be read", encoding="utf-8")
    (root / "normal.txt").write_text("body unchanged", encoding="utf-8")
    (root / "salary.csv").write_text("unchanged", encoding="utf-8")


def execute(root, **options):
    events = []
    scan(ScanOptions(str(root), **options), threading.Event(), events.append)
    return events, next(e for e in reversed(events) if e["kind"] == "finished")


def test_local_scan_original_paths_and_parent_attribution(tmp_path):
    fixture(tmp_path)
    events, result = execute(tmp_path, include_parents=True)
    hits = [h for e in events if e["kind"] == "hits" for h in e["rows"]]
    assert result["status"] == "complete"
    assert result["stats"]["checked"] == result["stats"]["visited"]
    assert any(h["is_dir"] and h["name"] == "张三" for h in hits)
    child = next(h for h in hits if h["name"] == "notes.txt")
    assert child["attribution"] == "parent"
    assert (tmp_path / "normal.txt").read_text() == "body unchanged"
    assert not list(tmp_path.glob("*.db"))


def test_default_checks_own_name_and_depth_is_explicit(tmp_path):
    fixture(tmp_path)
    events, _ = execute(tmp_path, recursive=False)
    assert not any(h["name"] == "notes.txt" for e in events if e["kind"] == "hits" for h in e["rows"])


def test_limits_are_partial_not_zero_risk_success(tmp_path):
    fixture(tmp_path)
    _, result = execute(tmp_path, max_entries=1)
    assert result["status"] == "partial" and result["reason"] == "entry_limit"
    _, result = execute(tmp_path, max_results=1)
    assert result["status"] == "partial" and result["reason"] == "result_limit"


def test_unreadable_and_private_state_are_visible_as_coverage(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / ".privacyfs-state.json").write_text("do not read")
    (tmp_path / "denied").mkdir()
    real = backend.os.scandir

    def guarded(path):
        if Path(path).name == "denied":
            raise PermissionError("PRIVATE_EXCEPTION")
        return real(path)

    monkeypatch.setattr(backend.os, "scandir", guarded)
    events, result = execute(tmp_path)
    assert result["stats"]["protected"] == 1 and result["stats"]["errors"] == 1
    assert result["status"] == "partial"
    assert "PRIVATE_EXCEPTION" not in json.dumps(events)


def test_yaml_cannot_enable_model_or_ner_implicitly(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    root = tmp_path / "source"
    root.mkdir()
    (root / "ordinary.txt").touch()
    rules = tmp_path / "rules.yaml"
    rules.write_text("use_llm: true\nllm_url: https://remote.invalid\nner_engine: hanlp\n")
    monkeypatch.setattr(backend, "review_names", lambda *a, **k: pytest.fail("unexpected model invocation"))
    _, result = execute(root, rules_path=str(rules))
    assert result["stats"]["llm_attempted"] == 0


def test_model_missing_verdict_and_budget_are_reported(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    for i in range(4):
        (tmp_path / f"zzunit{i}.dat").touch()
    monkeypatch.setattr(backend, "review_names", lambda names, model: {})
    _, result = execute(tmp_path, use_llm=True, llm_max=2)
    assert result["stats"]["llm_attempted"] == 2
    assert result["stats"]["llm_checked"] == 0
    assert result["stats"]["llm_unchecked"] >= 4
    assert result["status"] == "partial"


def test_cancel_during_scan_and_report_export_preserves_existing(tmp_path):
    fixture(tmp_path)
    event = threading.Event()
    events = []

    def receive(value):
        events.append(value)
        if value["kind"] == "progress":
            event.set()

    scan(ScanOptions(str(tmp_path)), event, receive)
    assert events[-1]["status"] == "cancelled"
    path = tmp_path / "export.json"
    report = {"status": "cancelled", "privacy_verified": False, "entries": []}
    export_report(path, report)
    with pytest.raises(FileExistsError):
        export_report(path, {"replaced": True})
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_spawn_controller_finishes_and_can_restart(tmp_path):
    fixture(tmp_path)
    controller = ScanController()
    try:
        for _ in range(2):
            controller.start(ScanOptions(str(tmp_path)))
            deadline = time.monotonic() + 25
            while controller.running and time.monotonic() < deadline:
                controller.poll()
                time.sleep(0.03)
            assert not controller.running
            assert controller.report["status"] == "complete"
            assert controller.rows
    finally:
        controller.close()
    assert controller.process is None


@pytest.mark.skipif(find_spec("privacyfs.gui.app") is None, reason="Slint frontend is local-only and excluded from version control")
def test_slint_window_compiles():
    slint = pytest.importorskip("slint", reason="optional GUI dependency not installed")
    from privacyfs.gui.app import UI_PATH
    components = slint.load_file(str(UI_PATH))
    assert components.AppWindow is not None


def test_forced_cancel_ends_unresponsive_worker_and_discards_queue(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    monkeypatch.setattr(backend, "_worker", _stalled_worker)
    controller = ScanController()
    try:
        controller.start(ScanOptions(str(tmp_path)))
        deadline = time.monotonic() + 10
        while not controller.report["stats"].get("visited") and time.monotonic() < deadline:
            controller.poll()
            time.sleep(0.03)
        assert controller.report["stats"]["visited"] == 1
        controller.cancel()
        deadline = time.monotonic() + 6
        while controller.running and time.monotonic() < deadline:
            controller.poll()
            time.sleep(0.03)
        assert not controller.running and controller.events is None
        assert controller.report["status"] == "cancelled"
        assert controller.report["reason"] == "terminated_after_cancel"
    finally:
        controller.close()


def test_no_body_reads_and_no_model_redirect_following(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    (tmp_path / "salary.txt").write_bytes(b"PRIVATE_BODY")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("source read"))
    monkeypatch.setattr(Path, "read_bytes", lambda *a, **k: pytest.fail("source read"))
    _, result = execute(tmp_path)
    assert result["status"] == "complete"
    calls = []

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append((host, port))
        def request(self, *args):
            pass
        def getresponse(self):
            return type("Response", (), {"status":302,"read":lambda self,n:b"redirect"})()
        def close(self):
            pass

    monkeypatch.setattr(backend.http.client, "HTTPConnection", Connection)
    with pytest.raises(ValueError):
        backend.review_names(["synthetic.txt"], "synthetic-model")
    assert calls == [("127.0.0.1",11434)]


def test_invalid_configuration_is_failed_and_bidi_display_is_escaped(tmp_path):
    from privacyfs.gui.backend import display_text
    _, result = execute(tmp_path, rules_path=str(tmp_path / "missing.yaml"))
    assert result["status"] == "failed"
    assert display_text("x\u202ey\n") == "x\\u202ey\\u000a"


def test_repeated_model_hit_survives_lru_eviction(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    monkeypatch.setattr(backend, "_CACHE_SIZE", 2)
    monkeypatch.setattr(backend, "review_names", lambda names, model: {
        n: "ORG" if n == "qqzzprivate.bin" else "NONE" for n in names})
    for directory in ("zzone", "zztwo", "zzthree"):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "qqzzprivate.bin").touch()
        for i in range(140):
            (folder / f"zzfiller{i}.bin").touch()
    events, result = execute(tmp_path, use_llm=True)
    hits = [h for e in events if e["kind"] == "hits" for h in e["rows"] if h["name"] == "qqzzprivate.bin"]
    assert len(hits) == 3
    assert all(any(s["source"] == "LocalLLM" for s in h["signals"]) for h in hits)


@pytest.mark.skipif(find_spec("privacyfs.gui.app") is None, reason="Slint frontend is local-only and excluded from version control")
def test_real_slint_callbacks_filter_page_and_select():
    pytest.importorskip("slint")
    from privacyfs.gui.app import Desktop
    desktop = Desktop()
    try:
        desktop.controller.rows.extend({"name":f"synthetic-{i}","path":f"C:/synthetic/{i}",
            "relative_path":f"folder/{i}","is_dir":i % 2 == 0,"attribution":"self",
            "signals":[{"category":"ORG","component":f"synthetic-{i}","surface":"synthetic",
                        "source":"Mock","is_self":True}]} for i in range(165))
        desktop.filter_rows()
        assert len(desktop.visible) == 80 and desktop.ui.next_enabled
        desktop.ui.next_page()
        assert desktop.visible[0]["name"] == "synthetic-80"
        desktop.ui.select_row(0)
        assert desktop.ui.has_selection and "Mock" in desktop.ui.detail_reason
        desktop.ui.type_filter = 2
        desktop.ui.filter_changed()
        assert all(r["is_dir"] for r in desktop.visible)
        desktop.ui.category_filter = 1
        desktop.ui.filter_changed()
        assert desktop.visible == []
    finally:
        desktop.timer.stop()
        desktop.controller.close()
