"""Large synthetic scans and bounded GUI result handling; no user disk scan."""
import json
import threading
import os
import queue
from types import SimpleNamespace

import pytest

from privacyfs.gui import backend


def test_default_scan_passes_old_entry_and_hit_limits(tmp_path, monkeypatch):
    total, sensitive = 1_000_005, 20_005

    class Entries:
        def __enter__(self):
            return (SimpleNamespace(name=f"private-{i}.txt" if i < sensitive else f"normal-{i}.txt",
                                    is_dir=lambda **kw: False) for i in range(total))
        def __exit__(self, *args):
            pass

    detector = SimpleNamespace(find=lambda name: [SimpleNamespace(category="PERSON", surface="private")]
                               if name.startswith("private-") else [])
    monkeypatch.setattr(backend.os, "scandir", lambda path: Entries())
    monkeypatch.setattr(backend, "_is_reparse", lambda entry: False)
    monkeypatch.setattr(backend, "_detectors_for", lambda rules: [detector])
    received, largest_batch, final = 0, 0, None

    def consume(event):
        nonlocal received, largest_batch, final
        if event["kind"] == "hits":
            received += len(event["rows"])
            largest_batch = max(largest_batch, len(event["rows"]))
        if event["kind"] == "finished":
            final = event

    backend.scan(backend.ScanOptions(str(tmp_path)), threading.Event(), consume)
    assert final["status"] == "complete"
    assert final["stats"]["checked"] == total
    assert received == sensitive
    assert largest_batch <= 128


def row(i):
    return {"name":f"synthetic-{i}", "path":f"C:/synthetic/{i}", "relative_path":str(i),
            "is_dir":i % 2 == 0, "attribution":"self",
            "signals":[{"category":"ORG", "surface":"needle" if i % 3 == 0 else "ordinary",
                        "component":"synthetic", "source":"Mock", "is_self":True}]}


def test_disk_results_filter_last_page_and_stream_export(tmp_path):
    from privacyfs.gui.results import ResultStore
    results = ResultStore()
    try:
        for start in range(0, 20_100, 100):
            results.extend(row(i) for i in range(start, start + 100))
        assert len(results) == 20_100
        page, count, pending, number = results.page(251, 80)
        assert count == 20_100 and not pending and number == 251
        assert page[-1]["name"] == "synthetic-20099"
        while True:
            page, count, pending, _ = results.page(0, 80, "needle", 2, ("ORG",))
            if not pending:
                break
        assert count == 3350 and all(r["is_dir"] for r in page)
        results.extend([row(20100)])
        page, count, pending, _ = results.page(0, 80, "needle", 2, ("ORG",))
        assert count == 3351 and not pending
        report = {"status":"complete", "entries":iter(results), "retained_hits":len(results)}
        target = tmp_path / "report.json"
        backend.export_report(target, report)
        exported = json.loads(target.read_text(encoding="utf-8"))
        assert exported["entries"][-1] == row(20100)
        assert len(exported["entries"]) == exported["retained_hits"] == 20101
    finally:
        results.close()


def test_pending_directories_spill_without_losing_or_merging_paths():
    from privacyfs.gui.results import DirectoryStack
    stack = DirectoryStack()
    try:
        for i in range(50_005):
            stack.append((f"synthetic/{i}", 2))
        assert len(stack.buffer) <= 128
        for i in reversed(range(50_005)):
            relative, depth = stack.pop()
            assert relative == f"synthetic/{i}" and depth == 2
        assert not stack
    finally:
        stack.close()


def test_result_storage_failure_stops_worker_and_reports_failure(monkeypatch):
    controller = backend.ScanController()
    class Process:
        pid = None
        def close(self):
            pass
    class Events(queue.Queue):
        def cancel_join_thread(self):
            pass
        def close(self):
            pass
    controller.process = Process()
    controller.cancel_event = threading.Event()
    controller.events = Events()
    controller.events.put({"kind":"hits", "rows":[row(1)]})
    def full(rows):
        raise OSError("SYNTHETIC_PRIVATE_DISK_PATH")
    monkeypatch.setattr(controller.rows, "extend", full)
    try:
        assert controller.poll()
        assert not controller.running and controller.cancel_event.is_set()
        assert controller.report["status"] == "failed"
        assert controller.report["reason"] == "result_storage_failed"
        assert "SYNTHETIC_PRIVATE" not in json.dumps(controller.report)
        assert controller.snapshot()["retained_hits"] == 0
    finally:
        controller.close()


def test_store_failure_rolls_back_count_and_contents(monkeypatch):
    from privacyfs.gui.results import ResultStore
    store = ResultStore()
    try:
        store.extend([row(0)])
        protect = store.provider.protect
        calls = 0
        def fail_second(payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("full")
            return protect(payload)
        monkeypatch.setattr(store.provider, "protect", fail_second)
        with pytest.raises(OSError):
            store.extend(row(i) for i in range(1, 257))
        assert len(store) == 1 and list(store) == [row(0)]
    finally:
        store.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI contract")
def test_windows_spool_has_no_plaintext_paths():
    from privacyfs.gui.results import ResultStore
    store = ResultStore()
    try:
        store.extend([row(1)])
        blob = store.db.execute("SELECT data FROM batches").fetchone()[0]
        assert b"synthetic" not in blob
        assert store.provider.name == "windows-dpapi-user"
        assert list(store) == [row(1)]
    finally:
        store.close()


def test_unfiltered_page_only_decodes_visible_batches(monkeypatch):
    from privacyfs.gui.results import ResultStore
    store = ResultStore()
    try:
        store.extend(row(i) for i in range(20_100))
        decode = store.decode
        decoded_rows = 0
        def counting(payload):
            nonlocal decoded_rows
            rows = decode(payload)
            decoded_rows += len(rows)
            return rows
        monkeypatch.setattr(store, "decode", counting)
        rows, count, pending, _ = store.page(250, 80)
        assert len(rows) == 80 and count == 20100 and not pending
        assert decoded_rows <= 256
    finally:
        store.close()


def test_finished_worker_queue_is_fully_drained_with_bounded_polls(monkeypatch):
    import itertools
    controller = backend.ScanController()
    class Process:
        pid = None
        def is_alive(self):
            return False
        def close(self):
            pass
    class Events(queue.Queue):
        def cancel_join_thread(self):
            pass
        def close(self):
            pass
    controller.process = Process()
    controller.cancel_event = threading.Event()
    controller.events = Events()
    for i in range(10):
        controller.events.put({"kind":"hits", "rows":[row(i)]})
    controller.events.put({"kind":"finished", "status":"complete"})
    clock = itertools.count()
    monkeypatch.setattr(backend.time, "monotonic", lambda: next(clock))
    try:
        for _ in range(11):
            controller.poll()
        assert not controller.running
        assert controller.report["status"] == "complete"
        assert list(controller.rows) == [row(i) for i in range(10)]
    finally:
        controller.close()


def test_directory_spill_preserves_real_descendants(tmp_path):
    for i in range(130):
        folder = tmp_path / f"synthetic-{i}"
        folder.mkdir()
        (folder / "salary.csv").touch()
    events = []
    backend.scan(backend.ScanOptions(str(tmp_path)), threading.Event(), events.append)
    final = events[-1]
    assert final["status"] == "complete"
    assert final["stats"]["visited"] == final["stats"]["checked"] == 260
    found = {r["path"] for event in events if event["kind"] == "hits" for r in event["rows"]}
    assert all(str(tmp_path / f"synthetic-{i}" / "salary.csv") in found for i in range(130))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI contract")
def test_repeated_protection_does_not_accumulate_python_types():
    import gc
    import tracemalloc
    from privacyfs.state_keys import WindowsDPAPI
    provider = WindowsDPAPI()
    provider.unprotect(provider.protect(b"synthetic warmup"))
    gc.collect()
    tracemalloc.start()
    try:
        for _ in range(400):
            assert provider.unprotect(provider.protect(b"synthetic batch")) == b"synthetic batch"
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
        assert retained < 512 * 1024
    finally:
        tracemalloc.stop()
