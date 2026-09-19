"""Embedded inference lifecycle; fake models only in the default test suite."""
import json
from importlib.util import find_spec
import os
import threading
import time

import pytest

from privacyfs.gui.backend import ScanController, ScanOptions


def _fake_model_worker(options, cancel, events):
    from privacyfs.gui import backend, embedded_ai
    class Model:
        def __init__(self, **kwargs):
            events.put({"kind":"progress", "factory_pid":os.getpid(), "factory_model":kwargs["model_path"],
                        "factory_context":kwargs["n_ctx"]})
            self.calls = 0
        def create_chat_completion(self, **kwargs):
            events.put({"kind":"progress", "factory_output_limit":kwargs["max_tokens"]})
            self.calls += 1
            records = json.loads(kwargs["messages"][1]["content"])["entries"]
            content = json.dumps({"items":[{"id":r["id"], "label":"UNCERTAIN",
                                           "reason":f"synthetic batch {self.calls}"} for r in records]})
            yield {"choices":[{"delta":{"content":content}, "finish_reason":None}]}
            yield {"choices":[{"delta":{}, "finish_reason":"stop"}]}
        def close(self):
            events.put({"kind":"progress", "model_closed":True, "model_batches":self.calls})
    embedded_ai._load_llama = lambda: Model
    backend.scan(options, cancel, events.put)


def _hung_model_worker(options, cancel, events):
    from privacyfs.gui import backend, embedded_ai
    class Model:
        def __init__(self, **kwargs):
            if "generating" not in kwargs["model_path"]:
                time.sleep(30)
        def create_chat_completion(self, **kwargs):
            time.sleep(30)
            yield {"choices":[]}
    embedded_ai._load_llama = lambda: Model
    backend.scan(options, cancel, events.put)


def fixture(tmp_path):
    model = tmp_path / "synthetic.gguf"
    model.write_bytes(b"GGUFsynthetic")
    root = tmp_path / "source"
    root.mkdir()
    for i in range(5):
        (root / f"record-{i}.txt").touch()
    return root, model


@pytest.mark.parametrize("context,output_limit", [(131072,32768), (8192,4096)])
def test_spawn_loads_once_reuses_batches_and_releases(tmp_path, monkeypatch, context, output_limit):
    from privacyfs.gui import backend
    root, model = fixture(tmp_path)
    monkeypatch.setattr(backend, "_worker", _fake_model_worker)
    controller = ScanController()
    try:
        controller.start(ScanOptions(str(root), directory_ai=True, ai_backend="embedded",
                                     ai_model_path=str(model), ai_batch_items=2, ai_context=context))
        deadline = time.monotonic() + 15
        while controller.running and time.monotonic() < deadline:
            controller.poll()
            time.sleep(0.02)
        assert not controller.running
        assert controller.report["status"] == "complete"
        assert controller.report["factory_pid"] != os.getpid()
        assert controller.report["factory_context"] == context
        assert controller.report["factory_output_limit"] == output_limit
        assert controller.report["ai_model_loads"] == 1
        assert controller.report["model_closed"]
        assert controller.report["model_batches"] == 3
        assert len(controller.rows) == 5
        assert controller.report["settings"]["ai_backend"] == "embedded"
    finally:
        controller.close()


@pytest.mark.parametrize("cancel_loading", [True, False], ids=["cancel", "timeout"])
@pytest.mark.parametrize("stage", ["loading", "generating"])
def test_loading_can_be_cancelled_or_timed_out(tmp_path, monkeypatch, cancel_loading, stage):
    from privacyfs.gui import backend
    root, model = fixture(tmp_path)
    if stage == "generating":
        model = tmp_path / "generating.gguf"
        model.write_bytes(b"GGUFsynthetic")
    monkeypatch.setattr(backend, "_worker", _hung_model_worker)
    controller = ScanController()
    try:
        controller.start(ScanOptions(str(root), directory_ai=True, ai_backend="embedded",
                                     ai_model_path=str(model), ai_worker_timeout=2))
        deadline = time.monotonic() + 12
        while controller.report.get("engine_state") != stage and time.monotonic() < deadline:
            controller.poll()
            time.sleep(0.02)
        assert controller.report.get("engine_state") == stage
        if cancel_loading:
            controller.cancel()
        while controller.running and time.monotonic() < deadline:
            controller.poll()
            time.sleep(0.02)
        assert not controller.running
        assert controller.report["status"] == ("cancelled" if cancel_loading else "failed")
        if not cancel_loading:
            assert controller.report["reason"] == "ai_worker_timeout"
        assert not controller.rows
    finally:
        controller.close()


def test_normal_gui_options_do_not_import_llama(monkeypatch):
    from privacyfs.gui import embedded_ai
    monkeypatch.setattr(embedded_ai, "_load_llama", lambda: pytest.fail("native model imported in GUI"))
    ScanOptions("synthetic")
    controller = ScanController()
    controller.close()


def test_context_supports_128k_and_preserves_explicit_smaller_values():
    assert ScanOptions("synthetic").ai_context == 131072
    assert ScanOptions("synthetic", ai_context=131072).ai_context == 131072
    assert ScanOptions("synthetic", ai_context=8192).ai_context == 8192
    with pytest.raises(ValueError):
        ScanOptions("synthetic", ai_context=131073)


@pytest.mark.parametrize("path", ["https://example.invalid/model.gguf", "\\\\remote\\models\\model.gguf"])
def test_network_model_paths_rejected(path):
    with pytest.raises(ValueError):
        ScanOptions("synthetic", directory_ai=True, ai_backend="embedded", ai_model_path=path)


@pytest.mark.parametrize("valid_header", [True, False], ids=["missing-runtime", "bad-file"])
def test_initialization_failures_are_explicit_and_never_use_http(tmp_path, monkeypatch, valid_header):
    from privacyfs.gui import backend, embedded_ai, directory_ai
    root, model = fixture(tmp_path)
    if not valid_header:
        model.write_bytes(b"not-a-model")
    def unavailable():
        raise embedded_ai.LocalModelError("ai_dependency_missing")
    monkeypatch.setattr(embedded_ai, "_load_llama", unavailable)
    monkeypatch.setattr(directory_ai, "review_records", lambda *a: pytest.fail("HTTP fallback"))
    events = []
    backend.scan(ScanOptions(str(root), directory_ai=True, ai_model_path=str(model)), threading.Event(), events.append)
    assert events[-1]["status"] == "failed"
    assert events[-1]["reason"] == ("ai_dependency_missing" if valid_header else "ai_model_file_invalid")
    assert events[-1]["stats"]["ai_checked"] == 0


@pytest.mark.skipif(find_spec("privacyfs.gui.app") is None, reason="Slint frontend is local-only and excluded from version control")
def test_real_gui_passes_selected_model_to_worker(tmp_path, monkeypatch):
    pytest.importorskip("slint")
    from privacyfs.gui.app import Desktop
    root, model = fixture(tmp_path)
    desktop = Desktop()
    captured = []
    try:
        desktop.ui.root_path = str(root)
        desktop.ui.directory_ai = True
        desktop.ui.ai_model_path = str(model)
        monkeypatch.setattr(desktop.controller, "start", captured.append)
        desktop.start()
        assert len(captured) == 1
        assert captured[0].ai_backend == "embedded"
        assert captured[0].ai_model_path == str(model)
        assert not captured[0].use_llm
    finally:
        desktop.timer.stop()
        desktop.controller.close()


def test_model_inventory_keeps_readable_unicode_and_escapes_invalid_codepoints():
    from privacyfs.gui.directory_ai import inventory_text
    records = [{"id":"E1", "path":"张三/资料.txt", "kind":"file"},
               {"id":"E2", "path":"invalid-\ud800.txt", "kind":"file"}]
    text = inventory_text(records)
    assert "张三/资料.txt" in text
    assert json.loads(text)["entries"] == records
    text.encode("utf-8")


def test_binding_template_uses_non_thinking_branch_without_loading_weights():
    pytest.importorskip("llama_cpp", reason="optional local-ai binding is not installed")
    from privacyfs.gui.embedded_ai import _configure_chat_template
    class Model:
        metadata = {"tokenizer.chat_template": "{{ bos_token }}{% if enable_thinking is defined and not enable_thinking %}JSON{% else %}THINK{% endif %}"}
        def token_eos(self):
            return 0
        def token_bos(self):
            return 1
        def detokenize(self, tokens, special=False):
            assert special
            return b"EOS" if tokens == [0] else b"BOS"
    model = Model()
    formatter = _configure_chat_template(model)
    assert formatter(messages=[]).prompt == "BOSJSON"
    assert callable(model.chat_handler)
