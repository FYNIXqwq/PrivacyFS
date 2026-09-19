"""Synthetic review workbench lifecycle and private report contracts."""
import csv
import json

import pytest


def row(name="synthetic.txt"):
    return {"path":"C:/synthetic/" + name, "relative_path":name, "name":name,
            "is_dir":False, "attribution":"context", "entry_id":"E1",
            "signals":[{"category":"PERSON", "surface":name, "component":name,
                        "source":"DirectoryAI", "is_self":False, "reason":"synthetic reason"}]}


def complete(controller):
    for _ in range(10000):
        controller.poll()
        if controller.job is None:
            return
    pytest.fail("file job did not finish")


def populated():
    from privacyfs.workbench.model import WorkbenchController
    controller = WorkbenchController()
    controller.scan.rows.extend([row("one.txt"), row("two.txt"), row("three.txt")])
    controller.scan.report = {"status":"partial", "privacy_verified":False, "root":"C:/synthetic",
                              "stats":{"visited":5, "ai_checked":3, "ai_unchecked":2}, "settings":{}}
    controller.dirty = True
    return controller


def test_review_undo_and_filtered_page_keep_original_verdict():
    c = populated()
    try:
        c.mark([1,2], "false_positive", "software sample")
        assert c.rows.counts()["false_positive"] == 2
        rows, count, pending, _ = c.rows.review_page(0, 80, state="false_positive")
        assert count == 2 and not pending
        assert rows[0]["signals"][0]["category"] == "PERSON"
        c.undo()
        assert c.rows.counts()["pending"] == 3
    finally:
        c.close()


def test_save_reopen_preserves_review_and_partial_state(tmp_path):
    from privacyfs.workbench.model import WorkbenchController
    c, loaded = populated(), WorkbenchController()
    target = tmp_path / "review.pfsreview"
    try:
        c.mark([2], "confirmed", "local note")
        c.save(target)
        complete(c)
        assert target.exists() and not c.dirty
        loaded.open(target)
        complete(loaded)
        assert len(loaded.rows) == 3 and loaded.rows.counts()["confirmed"] == 1
        assert loaded.rows.decision(2)["note"] == "local note"
        assert loaded.report["status"] == "partial"
        assert loaded.report["privacy_verified"] is False and loaded.reopened
        if __import__("os").name == "nt":
            assert b"synthetic" not in target.read_bytes() and b"local note" not in target.read_bytes()
    finally:
        c.close()
        loaded.close()


def test_bad_session_load_preserves_current_results(tmp_path):
    c = populated()
    path = tmp_path / "corrupt.pfsreview"
    path.write_bytes(b"broken")
    try:
        c.open(path, discard=True)
        complete(c)
        assert c.last_error and len(c.rows) == 3 and c.dirty
    finally:
        c.close()


def test_exports_are_explicit_scope_and_formula_safe(tmp_path):
    c = populated()
    try:
        c.mark([1], "confirmed", "=HYPERLINK(\"bad\")")
        c.export(tmp_path / "out.json", "json", "confirmed")
        complete(c)
        report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        assert len(report["entries"]) == 1
        assert report["candidate_count"] == 3 and report["exported_count"] == 1
        assert report["status"] == "partial" and report["privacy_verified"] is False
        c.export(tmp_path / "out.csv", "csv", "confirmed")
        complete(c)
        with (tmp_path / "out.csv").open(encoding="utf-8-sig", newline="") as stream:
            exported = list(csv.DictReader(stream))
        assert exported[0]["review_note"].startswith("'=")
        assert c.dirty  # Export is not a resumable session save.
    finally:
        c.close()


def test_cancelled_export_and_existing_target_are_preserved(tmp_path):
    c = populated()
    target = tmp_path / "existing.json"
    target.write_text("original", encoding="utf-8")
    try:
        c.export(target, "json", "all")
        complete(c)
        assert c.last_error and target.read_text(encoding="utf-8") == "original"
        fresh = tmp_path / "new.json"
        c.export(fresh, "json", "all")
        c.cancel_file_job()
        assert not fresh.exists()
    finally:
        c.close()


@pytest.mark.parametrize("corruption", ["truncate", "tamper"])
def test_corrupt_encrypted_session_is_transactional(tmp_path, corruption):
    c = populated()
    try:
        valid = tmp_path / "valid.pfsreview"
        c.save(valid)
        complete(c)
        data = valid.read_bytes()
        bad = tmp_path / "bad.pfsreview"
        bad.write_bytes(data[:-13] if corruption == "truncate" else data[:-1] + bytes([data[-1] ^ 1]))
        c.mark([1], "confirmed", "keep this review")
        original = c.rows
        c.open(bad, discard=True)
        complete(c)
        assert c.last_error and c.rows is original and c.dirty
        assert c.rows.decision(1)["note"] == "keep this review"
    finally:
        c.close()


def test_no_silent_discard_and_page_decision_preserves_notes(tmp_path):
    c = populated()
    try:
        with pytest.raises(ValueError):
            c.open(tmp_path / "unused")
        c.mark([1], "defer", "individual note")
        c.mark([1,2,3], "false_positive", None)
        assert c.rows.decision(1)["note"] == "individual note"
        c.undo()
        assert c.rows.state(1) == "defer" and c.rows.state(2) == "pending"
    finally:
        c.close()


def test_many_candidates_paginate_and_reopen_without_truncation(tmp_path):
    from privacyfs.workbench.model import WorkbenchController
    c, restored = populated(), WorkbenchController()
    try:
        c.rows.extend(row(f"file-{i}.txt") for i in range(20_010))
        c.mark([20_013], "confirmed", "last candidate")
        page, count, pending, number = c.rows.review_page(250, 80)
        assert page[-1]["human_review"]["note"] == "last candidate"
        assert count == 20013 and not pending
        target = tmp_path / "many.pfsreview"
        c.save(target)
        complete(c)
        restored.open(target)
        complete(restored)
        assert len(restored.rows) == 20013
        assert restored.rows.decision(20013)["note"] == "last candidate"
    finally:
        c.close()
        restored.close()


def test_tk_workflow_actions_and_close_save(tmp_path, monkeypatch):
    from privacyfs.workbench import app
    try:
        desktop = app.Workbench(show=False)
    except app.tk.TclError:
        pytest.skip("interactive Tk display unavailable")
    try:
        desktop.controller.close()
        desktop.controller = populated()
        assert desktop.context.get() == "131072"
        context_spin = next(w for w in desktop.config_widgets if w.winfo_class() == "TSpinbox"
                            and str(w.cget("textvariable")) == str(desktop.context))
        assert float(context_spin.cget("to")) == 131072
        desktop.model_path.set(str(tmp_path / "synthetic.gguf"))
        assert desktop.options().ai_context == 131072
        assert desktop.options().enumeration_mode == "ntfs"
        desktop.refresh()
        desktop.root.update_idletasks()
        desktop.tree.selection_set("1")
        desktop.select()
        desktop.note.insert("1.0", "review note")
        desktop.mark("confirmed")
        assert desktop.controller.rows.state(1) == "confirmed"
        desktop.state_filter.set("确认隐私")
        desktop.filter_changed()
        assert list(desktop.visible) == [1]
        target = tmp_path / "ui.pfsreview"
        monkeypatch.setattr(app.filedialog, "asksaveasfilename", lambda **kw: str(target))
        monkeypatch.setattr(app.messagebox, "askyesnocancel", lambda *args, **kw: True)
        desktop.close_request()
        for _ in range(100):
            if desktop._closed:
                break
            desktop.tick()
        assert desktop._closed and target.exists()
    finally:
        if not desktop._closed:
            desktop.shutdown()
