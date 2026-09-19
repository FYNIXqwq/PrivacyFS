import json
import queue
import threading
import time
import os
from pathlib import Path

import pytest

from privacyfs.gui.backend import ScanOptions, scan
from privacyfs.gui.mark_tools import dispatch_tools
from privacyfs.workbench.tree_store import TreeStore


def test_ntfs_preparation_metrics_are_not_ai_counts():
    from privacyfs.workbench import app
    try:
        desktop = app.Workbench(show=False)
    except app.tk.TclError:
        pytest.skip("Tk display unavailable")
    try:
        desktop.controller.scan.report = {"enumeration_backend":"ntfs_preparing", "stats":{
            "visited":0, "ntfs_records":12500, "ntfs_directories":240, "ntfs_link_names":3600}}
        desktop.refresh()
        assert desktop.metrics["visited"].get() == "12,500"
        assert desktop.metric_cards["visited"].cget("text") == "已读 NTFS 记录"
        assert desktop.metrics["checked"].get() == "240"
        assert desktop.metrics["unchecked"].get() == "3,600"
        assert desktop.metrics["hits"].get() == "—"
        desktop.controller.scan.report["stats"]["ntfs_preview_directories"] = 18345
        desktop.refresh()
        assert desktop.metric_cards["checked"].cget("text") == "已整理预览目录"
        assert desktop.metrics["checked"].get() == "18,345"
        desktop.controller.scan.report = {"enumeration_backend":"ntfs", "stats":{"visited":100,"ai_checked":20,"ai_unchecked":80}}
        desktop.refresh()
        assert desktop.metric_cards["visited"].cget("text") == "已遍历"
        assert desktop.metrics["visited"].get() == "100"
        assert desktop.metrics["checked"].get() == "20"
    finally:
        desktop.shutdown()


@pytest.mark.parametrize("verification_failure", [False, True])
def test_preliminary_names_do_not_reach_ai_until_verification(tmp_path, monkeypatch, verification_failure):
    from privacyfs.ntfs import index
    from privacyfs.gui.embedded_ai import EmbeddedModel
    from collections import Counter
    calls, events, seen = [], [], []
    class Inventory:
        def __init__(self, preliminary):
            self.preliminary, self.stats = preliminary, Counter()
        def entries(self):
            yield Path("unverified.txt" if self.preliminary else "verified.txt"), False, 0
        def close(self):
            pass
    def prepare(*args, **kwargs):
        preview = kwargs.get("preliminary", False)
        calls.append(preview)
        if not preview and verification_failure:
            raise index.NtfsUnavailable("journal_gap")
        return Inventory(preview)
    monkeypatch.setattr(index, "try_inventory", prepare)
    def review(self, records):
        assert calls == [True,False]
        seen.extend(r["path"] for r in records)
        return {r["id"]:{"label":"NONE","reason":""} for r in records}
    monkeypatch.setattr(EmbeddedModel, "review", review)
    commands = queue.Queue()
    def emit(event):
        events.append(event)
        if event.get("stage") == "preview":
            assert not event["inventory_complete"] and not event["inventory_verified"]
            assert not seen and calls == [True]
            commands.put(ScanOptions(str(tmp_path), workflow=True, enumeration_mode="ntfs", directory_ai=True, ai_model_path="fake.gguf"))
    scan(ScanOptions(str(tmp_path), workflow=True, enumeration_mode="ntfs"), threading.Event(), emit, commands)
    if verification_failure:
        assert not seen
        assert events[-1]["status"] == "failed"
        assert not any(e["kind"] == "inventory_reset" for e in events)
        return
    assert seen == ["verified.txt"]
    assert events[-1]["status"] == "complete"
    assert any(e["kind"] == "inventory_reset" for e in events)
    assert any(e.get("inventory_verified") is True for e in events)


def _tool_worker(options, cancel, events, commands):
    from privacyfs.gui import embedded_ai
    class Model:
        def __init__(self, **kwargs):
            events.put({"kind":"progress", "model_pid":os.getpid()})
        def create_chat_completion(self, **kwargs):
            assert kwargs["max_tokens"] == 32768
            assert "tool_calls" in kwargs["response_format"]["schema"]["properties"]
            records = json.loads(kwargs["messages"][1]["content"])["entries"]
            calls = [{"name":"mark_privacy", "arguments":{"id":r["id"],"label":"UNCERTAIN","reason":"合成测试"}} for r in records]
            yield {"choices":[{"delta":{"content":json.dumps({"tool_calls":calls})},"finish_reason":"stop"}]}
        def close(self):
            pass
    embedded_ai._load_llama = lambda: Model
    scan(options, cancel, events.put, commands)


def test_spawn_preview_tool_calls_and_live_tree(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    root = tmp_path / "source"
    root.mkdir()
    (root / "one.txt").touch()
    model = tmp_path / "fake.gguf"
    model.write_bytes(b"GGUF")
    monkeypatch.setattr(backend, "_worker", _tool_worker)
    controller = backend.ScanController()
    try:
        controller.start(ScanOptions(str(root), workflow=True))
        deadline = time.monotonic() + 20
        while controller.report.get("stage") != "preview" and time.monotonic() < deadline:
            controller.poll()
            time.sleep(.01)
        assert controller.report["stage"] == "preview"
        assert len(controller.inventory) == 1 and len(controller.rows) == 0
        assert "model_pid" not in controller.report
        controller.begin_review(ScanOptions(str(root), workflow=True, directory_ai=True, ai_model_path=str(model)))
        while controller.running and time.monotonic() < deadline:
            controller.poll()
            time.sleep(.01)
        assert not controller.running and controller.report["status"] == "complete"
        assert controller.report["model_pid"] != os.getpid()
        assert controller.report["stats"]["ai_tool_calls"] == 1
        assert controller.inventory.children()[0]["verdict"]["label"] == "UNCERTAIN"
        assert len(controller.rows) == 1
    finally:
        controller.close()


def test_tool_dispatch_rejects_unknown_ids_actions_and_duplicates():
    records = [{"id":"E1","path":"LICENSE-MIT","kind":"file"}]
    call = {"name":"record_clear","arguments":{"id":"E1","label":"NONE","reason":""}}
    assert dispatch_tools(json.dumps({"tool_calls":[call]}), records)["E1"]["label"] == "NONE"
    for calls in ([call,call], [{**call,"name":"delete_file"}],
                  [{**call,"arguments":{**call["arguments"],"id":"E2"}}],
                  [{**call,"name":"mark_privacy"}]):
        with pytest.raises(ValueError):
            dispatch_tools(json.dumps({"tool_calls":calls}), records)
    assert dispatch_tools('{"tool_calls":[]}', records) == {}


def test_preview_before_model_and_same_inventory(tmp_path, monkeypatch):
    from privacyfs.gui.embedded_ai import EmbeddedModel
    (tmp_path / "LICENSE-MIT").write_text("not read")
    (tmp_path / "张三.txt").write_text("not read")
    commands, cancel, events, seen = queue.Queue(), threading.Event(), [], []
    def review(self, records):
        seen.extend(r["path"] for r in records)
        return {r["id"]:{"label":"PERSON" if "张三" in r["path"] else "NONE", "reason":"姓名"} for r in records}
    monkeypatch.setattr(EmbeddedModel, "review", review)
    def emit(event):
        events.append(event)
        if event.get("stage") == "preview":
            assert not seen
            assert sum(len(e.get("rows",[])) for e in events if e["kind"] == "inventory") == 2
            # Later filesystem changes must not alter the displayed inventory.
            (tmp_path / "late.txt").write_text("not included")
            commands.put(ScanOptions(str(tmp_path), workflow=True, directory_ai=True, ai_model_path="fake.gguf"))
    scan(ScanOptions(str(tmp_path), workflow=True, enumeration_mode="walk"), cancel, emit, commands)
    assert set(seen) == {"LICENSE-MIT","张三.txt"}
    assert events[-1]["status"] == "complete"
    assert any(e["kind"] == "marks" for e in events)
    hits = [r for e in events if e["kind"] == "hits" for r in e["rows"]]
    assert len(hits) == 1 and hits[0]["name"] == "张三.txt"


def test_cancel_in_preview_never_loads_model(tmp_path, monkeypatch):
    from privacyfs.gui.embedded_ai import EmbeddedModel
    monkeypatch.setattr(EmbeddedModel, "load", lambda self: pytest.fail("unexpected inference"))
    cancel, events = threading.Event(), []
    def emit(event):
        events.append(event)
        if event.get("stage") == "preview":
            cancel.set()
    scan(ScanOptions(str(tmp_path), workflow=True), cancel, emit, queue.Queue())
    assert events[-1]["status"] == "cancelled"


@pytest.mark.parametrize("storage", ["disk", "memory", "spill", "hybrid"])
def test_tree_paging_and_live_marks(storage):
    from privacyfs.workbench.tree_store import PreviewTreeStore
    store = TreeStore() if storage == "disk" else PreviewTreeStore(
        memory_budget=1 if storage == "spill" else 30000 if storage == "hybrid" else 512*1024*1024)
    try:
        source = ({"entry_id":f"E{i+1}","relative_path":f"folder/file{i}.txt", "name":f"file{i}.txt", "is_dir":False} for i in range(205))
        if storage == "hybrid":
            import itertools
            store.extend(itertools.islice(source,80))
        store.extend(source)
        first = store.children("folder")
        second = store.children("folder", int(first[-1]["entry_id"][1:]))
        assert len(first) == len(second) == 80
        assert first[-1]["entry_id"] != second[0]["entry_id"]
        assert store.previous("folder", 160) == 80
        assert store.previous("folder", 80) == 0
        store.mark({"E1":{"label":"PERSON","reason":"synthetic"}})
        assert store.children("folder")[0]["verdict"]["label"] == "PERSON"
        store.mark({"E205":{"label":"UNCERTAIN","reason":"last"}})
        assert store.children("folder", 204)[0]["verdict"]["label"] == "UNCERTAIN"
        with pytest.raises(ValueError):
            store.mark({"E999":{"label":"NONE","reason":""}})
    finally:
        store.close()
