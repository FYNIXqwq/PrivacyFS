"""Synthetic NTFS records and aliases; never enumerate a real system volume."""
from collections import Counter
from pathlib import Path
import struct
import threading

import pytest


def record_bytes(fid, parent, name, attrs=0, reason=0, usn=1):
    encoded = name.encode("utf-16-le", "surrogatepass")
    size = (60 + len(encoded) + 7) & ~7
    return struct.pack("<IHHQQqqIIIIHH", size, 2, 0, fid, parent, usn, 0, reason, 0, 0, attrs, len(encoded), 60) + encoded + b"\0"*(size-60-len(encoded))


def test_usn_parser_keeps_unsigned_ids_unicode_and_validates_bounds():
    from privacyfs.ntfs.native import parse_usn_records, NtfsUnavailable
    data = record_bytes(2**63+7, 5, "张三.txt")
    item = list(parse_usn_records(data))[0]
    assert item.file_id == 2**63+7 and item.name == "张三.txt"
    for broken in (data[:-1], b"\0"*64, data[:4]+b"\x03\x00"+data[6:]):
        with pytest.raises(NtfsUnavailable):
            list(parse_usn_records(broken))
    with pytest.raises(NtfsUnavailable):
        list(parse_usn_records(record_bytes(200, 5, "..\\outside")))
    from privacyfs.gui.backend import display_text
    invalid_unicode = list(parse_usn_records(record_bytes(200,5,"name-\ud800")))[0]
    assert display_text(invalid_unicode.name) == "name-\\ud800"


class FakeVolume:
    root_id = 5
    def __init__(self, root):
        from privacyfs.ntfs.native import Record
        self.nodes = [Record(100,5,0x10,0,"allowed"), Record(101,5,0x10,0,"excluded"),
                      Record(102,5,0x10,0,"private"), Record(103,5,0x410,0,"junction"),
                      Record(200,101,0,0,"primary.txt"), Record(201,100,0,0,"single.txt")]
        self.updates = []
        self.links = {200:(0,[(101,"primary.txt"),(100,"alias-one.txt"),(100,"alias-two.txt")]),
                      201:(0,[(100,"single.txt")])}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def journal(self):
        from privacyfs.ntfs.native import Journal
        self.journal_calls = getattr(self, "journal_calls", 0) + 1
        return Journal(1,0,10 if self.journal_calls == 1 else 100)
    def records(self, cancel):
        yield from self.nodes
    def changes(self, start, end, cancel):
        yield from (r for r in self.updates if start.next_usn <= r.usn < end.next_usn)
    def current_record(self, identifier):
        return next((r for r in self.updates if r.file_id == identifier), None)
    def gate_directory(self, path, identifier):
        return "ok"
    def file_links(self, identifier):
        return self.links[identifier]


@pytest.mark.parametrize("memory", [False, True])
def test_preliminary_inventory_never_opens_files_or_probes_directories(tmp_path, memory):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.preview import build_preview
    class MetadataOnly(FakeVolume):
        def file_links(self, identifier):
            pytest.fail("per-file IO before preview")
        def gate_directory(self, *args):
            pytest.fail("directory probe before preview")
        def journal(self):
            pytest.fail("preview must not reconcile file changes")
    builder = build_preview if memory else build_inventory
    inventory = builder(tmp_path, [], threading.Event(), lambda **kw: None,
        source_factory=MetadataOnly, scope=Path("allowed"), **({} if memory else {"preliminary":True}))
    try:
        assert inventory.preliminary
        assert inventory.stats["ntfs_preview_only"] == 1
        assert {p.as_posix() for p,_,_ in inventory.entries()} == {"single.txt"}
        assert inventory.stats["ntfs_link_names"] == 0
    finally:
        inventory.close()


def test_memory_preview_budget_spills_without_truncating(tmp_path):
    from privacyfs.ntfs.preview import build_preview
    previews = [build_preview(tmp_path, [], threading.Event(), lambda **kw: None,
                source_factory=FakeVolume, memory_budget=budget) for budget in (100000, 1)]
    try:
        assert {str(p) for p,_,_ in previews[0].entries()} == {str(p) for p,_,_ in previews[1].entries()}
        assert not hasattr(previews[0], "database_path") and hasattr(previews[1], "database_path")
    finally:
        for preview in previews:
            preview.close()


def test_disk_hierarchy_does_not_decode_children_just_to_queue_ids(tmp_path, monkeypatch):
    from privacyfs.ntfs.index import build_inventory, Inventory
    monkeypatch.setattr(Inventory, "children", lambda *args: pytest.fail("decoded children during root traversal"))
    events = []
    inventory = build_inventory(tmp_path, [], threading.Event(), lambda **kw: events.append(kw),
                                source_factory=FakeVolume, preliminary=True)
    try:
        assert any(e.get("ntfs_preview_directories",0) > 0 for e in events)
        assert len(list(inventory.entries())) == 5
    finally:
        inventory.close()


def test_cancel_interrupts_large_sql_directory_queue(tmp_path, monkeypatch):
    from privacyfs.ntfs import index
    from privacyfs.ntfs.native import Record, NtfsCancelled
    from types import SimpleNamespace
    import itertools
    clock = itertools.count()
    monkeypatch.setattr(index, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    class Wide(FakeVolume):
        def records(self, cancel):
            for i in range(4000):
                yield Record(1000+i,5,0x10,0,f"folder-{i}")
    cancel = threading.Event()
    heartbeats = []
    def notify(**values):
        if values["phase"].startswith("构建预览") and "ntfs_records" not in values and "ntfs_preview_directories" not in values:
            heartbeats.append(values)
            cancel.set()
    with pytest.raises(NtfsCancelled):
        index.build_inventory(tmp_path, [], cancel, notify, source_factory=Wide, preliminary=True)
    assert heartbeats


def test_small_inventory_reports_read_progress_before_preparation_finishes(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    events = []
    inventory = build_inventory(tmp_path, [], threading.Event(), lambda **kw: events.append(kw), source_factory=FakeVolume)
    try:
        assert any(e["phase"] == "读取 NTFS 名称索引" and e.get("ntfs_records",0) > 0 for e in events)
    finally:
        inventory.close()


def test_selected_subdirectory_uses_volume_graph_and_keeps_cross_boundary_aliases(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    inventory = build_inventory(tmp_path, [], threading.Event(), lambda **kw: None,
                                source_factory=FakeVolume, scope=Path("allowed"))
    try:
        assert {p.as_posix() for p,_,_ in inventory.entries()} == {"alias-one.txt", "alias-two.txt", "single.txt"}
    finally:
        inventory.close()


def test_subdirectory_scope_cannot_cross_private_ancestor(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import Record, NtfsUnavailable
    def factory(root):
        source = FakeVolume(root)
        source.nodes += [Record(104,100,0x10,0,"nested"), Record(300,100,0,0,".privacyfs-state.json")]
        return source
    with pytest.raises(NtfsUnavailable, match="protected_scope"):
        build_inventory(tmp_path, [], threading.Event(), lambda **kw: None,
                        source_factory=factory, scope=Path("allowed/nested"))


def test_required_ntfs_failure_does_not_walk_or_publish_preview(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    from privacyfs.ntfs import index
    def denied(*args, **kwargs):
        assert kwargs["allow_subdirectory"]
        raise index.NtfsUnavailable("access_denied")
    monkeypatch.setattr(index, "try_inventory", denied)
    monkeypatch.setattr(backend.os, "scandir", lambda *args: pytest.fail("unexpected recursive fallback"))
    events = []
    backend.scan(backend.ScanOptions(str(tmp_path), enumeration_mode="ntfs", workflow=True),
                 threading.Event(), events.append)
    assert events[-1]["status"] == "failed"
    assert events[-1]["reason"] == "ntfs_access_denied"
    assert events[-1]["enumeration_backend"] == "ntfs_unavailable"
    assert not any(e.get("stage") == "preview" or e["kind"] == "inventory" for e in events)


def test_index_preserves_all_aliases_and_filters_private_subtrees(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    private = tmp_path / "private"
    private.mkdir()
    (private / ".privacyfs-state.json").write_text("PRIVATE BODY")
    inventory = build_inventory(tmp_path, ["excluded"], threading.Event(), lambda **kw: None,
                                source_factory=FakeVolume)
    try:
        paths = {p.as_posix() for p, _, _ in inventory.entries()}
        assert paths == {"allowed", "allowed/alias-one.txt", "allowed/alias-two.txt", "allowed/single.txt"}
        assert inventory.stats["protected"] == 1 and inventory.stats["reparse"] == 1
        assert b"alias-one" not in inventory.database_path.read_bytes()
        cache_dir = inventory.directory
    finally:
        inventory.close()
    assert not cache_dir.exists()
    assert (private / ".privacyfs-state.json").read_text() == "PRIVATE BODY"


def test_journal_update_changes_directory_path_before_emission(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import Record
    def factory(root):
        source = FakeVolume(root)
        source.updates = [Record(100,5,0x10,50,"renamed",0x2000)]
        return source
    inventory = build_inventory(tmp_path, ["excluded", "private", "junction"], threading.Event(), lambda **kw: None,
                                source_factory=factory)
    try:
        paths = {p.as_posix() for p,_,_ in inventory.entries()}
        assert "renamed/alias-one.txt" in paths and "allowed/alias-one.txt" not in paths
        assert inventory.stats["ntfs_journal_updates"] == 1
    finally:
        inventory.close()


def test_cancelled_ntfs_attempt_never_falls_back_to_walk(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    from privacyfs.ntfs import index
    event = threading.Event()
    def cancelled(*args, **kwargs):
        event.set()
        raise index.NtfsCancelled()
    monkeypatch.setattr(index, "try_inventory", cancelled)
    monkeypatch.setattr(backend.os, "scandir", lambda *a: pytest.fail("walk after cancellation"))
    events = []
    backend.scan(backend.ScanOptions(str(tmp_path)), event, events.append)
    assert events[-1]["status"] == "cancelled"


def test_unavailable_ntfs_falls_back_without_losing_entries(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    from privacyfs.ntfs import index
    from privacyfs.ntfs.native import NtfsUnavailable
    (tmp_path / "salary.csv").touch()
    def denied(*args, **kwargs):
        raise NtfsUnavailable("access_denied")
    monkeypatch.setattr(index, "try_inventory", denied)
    events = []
    backend.scan(backend.ScanOptions(str(tmp_path)), threading.Event(), events.append)
    final = events[-1]
    assert final["status"] == "complete" and final["stats"]["visited"] == 1
    assert final["enumeration_backend"] == "walk" and final["enumeration_fallback"] == "access_denied"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows native metadata API")
def test_real_hardlink_metadata_on_synthetic_files_only(tmp_path):
    import os
    from privacyfs.ntfs.native import WindowsAPI
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    original = tmp_path / "one" / "synthetic-long-name.txt"
    alias = tmp_path / "two" / "second-long-name.txt"
    original.write_bytes(b"SYNTHETIC CONTENT MUST NOT CHANGE")
    os.link(original, alias)
    api = WindowsAPI()
    handles = []
    try:
        handle = api.open_path(original)
        handles.append(handle)
        info, identifier = api.info(handle)
        expected = set()
        for directory, name in ((original.parent,original.name),(alias.parent,alias.name)):
            parent_handle = api.open_path(directory)
            handles.append(parent_handle)
            _, parent_id = api.info(parent_handle)
            expected.add((parent_id,name))
        assert info.links == 2 and set(api.links(handle)) == expected
        by_id = api.open_id(handles[1], identifier)
        handles.append(by_id)
        assert set(api.links(by_id)) == expected
    finally:
        for handle in handles:
            api.close(handle)
    assert original.read_bytes() == alias.read_bytes() == b"SYNTHETIC CONTENT MUST NOT CHANGE"


def test_subdirectory_selection_does_not_open_volume(tmp_path, monkeypatch):
    from privacyfs.ntfs import index
    from privacyfs.ntfs.native import NtfsUnavailable
    monkeypatch.setattr(index, "build_inventory", lambda *a, **kw: pytest.fail("volume opened for subdirectory"))
    with pytest.raises(NtfsUnavailable):
        index.try_inventory(tmp_path,True,[],threading.Event(),lambda **kw: None)


def test_explicit_walk_mode_never_attempts_ntfs(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    from privacyfs.ntfs import index
    monkeypatch.setattr(index, "try_inventory", lambda *a: pytest.fail("unexpected NTFS attempt"))
    (tmp_path / "salary.csv").touch()
    events = []
    backend.scan(backend.ScanOptions(str(tmp_path),enumeration_mode="walk"),threading.Event(),events.append)
    assert events[-1]["status"] == "complete" and events[-1]["stats"]["checked"] == 1


def test_ntfs_inventory_uses_existing_detector_and_emits_no_duplicates(tmp_path, monkeypatch):
    from privacyfs.gui import backend
    from privacyfs.ntfs import index
    class Prepared:
        stats = Counter(ntfs_records=2)
        closed = False
        def entries(self):
            yield Path("salary.csv"),False,0
            yield Path("salary-copy.csv"),False,0
        def close(self):
            self.closed = True
    prepared = Prepared()
    monkeypatch.setattr(index,"try_inventory",lambda *a: prepared)
    monkeypatch.setattr(backend.os,"scandir",lambda *a: pytest.fail("recursive walk during indexed emission"))
    events = []
    backend.scan(backend.ScanOptions(str(tmp_path)),threading.Event(),events.append)
    assert events[-1]["enumeration_backend"] == "ntfs"
    assert events[-1]["stats"]["visited"] == events[-1]["stats"]["checked"] == 2
    assert sum(len(e["rows"]) for e in events if e["kind"] == "hits") == 2 and prepared.closed


def test_same_file_case_distinct_aliases_are_not_merged(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    def factory(root):
        source = FakeVolume(root)
        source.links[200] = (0,[(100,"Name.txt"),(100,"name.txt")])
        return source
    inventory = build_inventory(tmp_path,["excluded","private","junction"],threading.Event(),lambda **kw: None,
                                source_factory=factory)
    try:
        paths = {p.as_posix() for p,_,_ in inventory.entries()}
        assert "allowed/Name.txt" in paths and "allowed/name.txt" in paths
    finally:
        inventory.close()
        inventory.close()


def test_graph_cycle_or_missing_parent_rejects_inventory(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import Record,NtfsUnavailable
    for parent in (100,99999):
        def factory(root):
            source = FakeVolume(root)
            source.nodes[0] = Record(100,parent,0x10,0,"allowed")
            return source
        with pytest.raises(NtfsUnavailable):
            build_inventory(tmp_path,[],threading.Event(),lambda **kw: None,source_factory=factory)


def test_alias_failure_does_not_publish_partial_index(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import NtfsUnavailable
    def factory(root):
        source = FakeVolume(root)
        def failed(identifier):
            raise NtfsUnavailable("hardlinks_unavailable")
        source.file_links = failed
        return source
    with pytest.raises(NtfsUnavailable, match="hardlinks_unavailable"):
        build_inventory(tmp_path,[],threading.Event(),lambda **kw: None,source_factory=factory)


def test_journal_gap_is_not_ignored():
    from privacyfs.ntfs.native import NativeVolume,Journal,NtfsUnavailable
    volume = object.__new__(NativeVolume)
    with pytest.raises(NtfsUnavailable, match="journal_gap"):
        list(volume.changes(Journal(1,0,10),Journal(2,0,20),threading.Event()))
    with pytest.raises(NtfsUnavailable, match="journal_gap"):
        list(volume.changes(Journal(1,0,10),Journal(1,15,20),threading.Event()))


def test_damaged_link_buffer_is_rejected():
    from privacyfs.ntfs.native import parse_links,NtfsUnavailable
    for data in (b"",struct.pack("<II",8,1),struct.pack("<II",0,0)):
        with pytest.raises(NtfsUnavailable):
            parse_links(data)


def test_marker_metadata_blocks_private_directory_even_without_live_probe(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import Record
    def factory(root):
        source = FakeVolume(root)
        source.nodes.append(Record(202,102,0,0,".privacyfs-state.json"))
        source.links[202] = (0,[(102,".privacyfs-state.json")])
        return source
    inventory = build_inventory(tmp_path,["excluded","junction"],threading.Event(),lambda **kw: None,source_factory=factory)
    try:
        assert "private" not in {p.as_posix() for p,_,_ in inventory.entries()}
        assert inventory.stats["protected"] == 1
    finally:
        inventory.close()


def test_new_marker_alias_invalidates_prepared_scope(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import NtfsUnavailable
    def factory(root):
        source = FakeVolume(root)
        source.links[201] = (0,[(100,".privacyfs-state.json")])
        return source
    with pytest.raises(NtfsUnavailable,match="privacy_boundary_changed"):
        build_inventory(tmp_path,[],threading.Event(),lambda **kw: None,source_factory=factory)


def test_late_structural_changes_are_reported_as_incomplete(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import Record,Journal
    class Changing(FakeVolume):
        def __init__(self,root):
            super().__init__(root)
            self.updates = [Record(100,5,0x10,150,"new-name",0x2000)]
        def journal(self):
            self.journal_calls = getattr(self,"journal_calls",0)+1
            return Journal(1,0,[10,100,200][self.journal_calls-1])
    inventory = build_inventory(tmp_path,["excluded","private","junction"],threading.Event(),lambda **kw: None,
                                source_factory=Changing)
    try:
        assert inventory.stats["ntfs_late_changes"] == 1
        assert inventory.stats["errors"] > 0
    finally:
        inventory.close()


def test_timestamp_only_updates_do_not_invalidate_names(tmp_path):
    from privacyfs.ntfs.index import build_inventory
    from privacyfs.ntfs.native import Record,Journal
    class Timestamps(FakeVolume):
        def __init__(self,root):
            super().__init__(root)
            self.updates = [Record(201,100,0,150,"single.txt",0x8000)]
        def journal(self):
            self.journal_calls = getattr(self,"journal_calls",0)+1
            return Journal(1,0,[10,100,200][self.journal_calls-1])
    inventory = build_inventory(tmp_path,["excluded","private","junction"],threading.Event(),lambda **kw: None,
                                source_factory=Timestamps)
    try:
        assert inventory.stats["ntfs_late_changes"] == inventory.stats["errors"] == 0
        assert "allowed/single.txt" in {p.as_posix() for p,_,_ in inventory.entries()}
    finally:
        inventory.close()
