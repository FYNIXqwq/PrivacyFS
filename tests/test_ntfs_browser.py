import json
import queue
import threading
import time
from pathlib import Path

import pytest

from privacyfs.ntfs.index import build_inventory, Inventory
from privacyfs.ntfs.native import Record
from privacyfs.ntfs.browser import browse
from privacyfs.workbench.remote_tree import RemoteTreeStore
from test_ntfs_index import FakeVolume


def test_lazy_index_never_walks_entire_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(Inventory, "queue_children", lambda *a: pytest.fail("global hierarchy walk"))
    monkeypatch.setattr(Inventory, "entries", lambda *a: pytest.fail("global path construction"))
    index = build_inventory(tmp_path, [], threading.Event(), lambda **kw: None,
                            source_factory=FakeVolume, preliminary=True, lazy=True)
    try:
        assert index.lazy and index.db.execute("SELECT count(*) FROM allowed").fetchone()[0] == 0
        root = browse(index)
        assert {r['name'] for r in root['rows']} == {'allowed','excluded','private'}
        allowed = next(r for r in root['rows'] if r['name'] == 'allowed')
        assert [r['name'] for r in browse(index, 'allowed', directory_id=allowed['_directory_id'])['rows']] == ['single.txt']
        with pytest.raises(Exception):
            browse(index, '.', directory_id=100)
    finally:
        index.close()


@pytest.mark.parametrize('private_ancestor',[False,True])
def test_selected_scope_resolves_by_id_without_enumerating_siblings(tmp_path, monkeypatch, private_ancestor):
    class Direct(FakeVolume):
        def __init__(self, root):
            super().__init__(root)
            if private_ancestor:
                self.nodes.append(Record(400,5,0,0,'.privacyfs-state.json'))
        def directory_identity(self, path):
            assert path == tmp_path / 'allowed'
            return 100
    monkeypatch.setattr(Inventory,'children',lambda *args: pytest.fail('scanned siblings'))
    if private_ancestor:
        from privacyfs.ntfs.native import NtfsUnavailable
        with pytest.raises(NtfsUnavailable,match='protected_scope'):
            build_inventory(tmp_path, [], threading.Event(), lambda **kw: None,
                            source_factory=Direct, scope=Path('allowed'), preliminary=True, lazy=True)
        return
    index = build_inventory(tmp_path, [], threading.Event(), lambda **kw: None,
                            source_factory=Direct, scope=Path('allowed'), preliminary=True, lazy=True)
    try:
        assert [r['name'] for r in browse(index)['rows']] == ['single.txt']
        with pytest.raises(Exception):
            browse(index,'../excluded',directory_id=101)
    finally:
        index.close()


def test_verification_reuses_index_without_enumerating_volume_again(tmp_path):
    calls = []
    class Once(FakeVolume):
        def records(self,cancel):
            calls.append('enumeration')
            assert len(calls) == 1
            yield from super().records(cancel)
    index = build_inventory(tmp_path, [], threading.Event(), lambda **kw: None,
                            source_factory=Once, preliminary=True, lazy=True)
    try:
        verified = build_inventory(tmp_path, [], threading.Event(), lambda **kw: None,
                                   source_factory=Once, existing=index)
        assert verified is index and not index.preliminary and not index.lazy
        assert len(calls) == 1
        assert 'allowed/alias-one.txt' in {p.as_posix() for p,_,_ in verified.entries()}
    finally:
        index.close()


def test_filtered_pages_continue_without_dropping_entries(tmp_path):
    class Wide(FakeVolume):
        def records(self, cancel):
            for i in range(4100):
                yield Record(1000+i,5,0,0, f'skip-{i}' if i<4000 else f'keep-{i}')
    index = build_inventory(tmp_path, ['skip-*'], threading.Event(), lambda **kw: None,
                            source_factory=Wide, preliminary=True, lazy=True)
    try:
        cursor, names, calls = 0, [], 0
        while True:
            page = browse(index, after=cursor, limit=81)
            calls += 1
            names.extend(r['name'] for r in page['rows'])
            cursor = page['cursor']
            if not page['pending'] and not page['rows']:
                break
        assert calls > 2
        assert names == [f'keep-{i}' for i in range(4000,4100)]
    finally:
        index.close()


def test_remote_cache_requests_only_pages_and_keeps_navigation():
    sent = []
    store = RemoteTreeStore(lambda q: sent.append(q) or True, 11673001, '5')
    assert store.children() == [] and len(sent) == 1
    assert store.children() == [] and len(sent) == 1
    key = sent[-1]['key']
    rows = [{'entry_id':f'P{i}', 'relative_path':f'item-{i}', 'name':f'item-{i}',
             'is_dir':False, 'verdict':None} for i in range(1,82)]
    store.accept(key, {'rows':rows,'cursor':81,'pending':False})
    assert len(store.children()) == 81
    assert store.previous('.',80) == 0
    store.children(after=80)
    assert sent[-1]['after'] == 80
    store.pause()
    assert not store.pending and len(store.children()) == 81
    assert len(store.cache) <= 8


def _lazy_worker(options, cancel, events, commands):
    from privacyfs.gui import backend
    from privacyfs.gui.embedded_ai import EmbeddedModel
    from privacyfs.ntfs import index
    def prepare(root, recursive, exclusions, cancel, notify, **kw):
        preliminary = kw.get('preliminary',False)
        return build_inventory(root,exclusions,cancel,notify,source_factory=FakeVolume,
                               preliminary=preliminary,lazy=preliminary,existing=kw.get('existing'))
    def review(self, records):
        assert all(r['id'].startswith('E') and '_directory_id' not in r for r in records)
        return {r['id']:{'label':'NONE','reason':''} for r in records}
    EmbeddedModel.review = review
    index.try_inventory = prepare
    backend.scan(options,cancel,events.put,commands)


@pytest.mark.parametrize('review_after_preview',[False,True])
@pytest.mark.parametrize('enumeration_mode',['ntfs','auto'])
def test_spawn_serves_preview_page_without_materializing_tree(tmp_path, monkeypatch, review_after_preview, enumeration_mode):
    from privacyfs.gui import backend
    monkeypatch.setattr(backend,'_worker',_lazy_worker)
    controller = backend.ScanController()
    try:
        controller.start(backend.ScanOptions(str(tmp_path),workflow=True,enumeration_mode=enumeration_mode,use_excludes=False))
        deadline = time.monotonic()+15
        while controller.report.get('stage') != 'preview' and time.monotonic()<deadline:
            controller.poll()
            time.sleep(.01)
        assert controller.report['stage'] == 'preview'
        assert isinstance(controller.inventory,RemoteTreeStore)
        assert controller.report['stats']['visited'] == 0
        rows = []
        while not rows and time.monotonic()<deadline:
            rows = controller.inventory.children()
            controller.poll()
            time.sleep(.01)
        assert {r['name'] for r in rows} == {'allowed','excluded','private'}
        assert 'root_directory_id' not in controller.report and 'page' not in controller.report
        assert controller.running and not controller.rows
        if review_after_preview:
            controller.begin_review(backend.ScanOptions(str(tmp_path),workflow=True,enumeration_mode=enumeration_mode,
                use_excludes=False,directory_ai=True,ai_model_path='fake.gguf'))
            while controller.running and time.monotonic()<deadline:
                controller.poll()
                time.sleep(.01)
            assert not controller.running and controller.report['status'] == 'complete'
            assert controller.report['stats']['ai_checked'] > 0
            assert controller.report['inventory_verified'] and not controller.report['lazy_preview']
            assert not isinstance(controller.inventory,RemoteTreeStore)
            return
        controller.cancel()
        while controller.running and time.monotonic()<deadline:
            controller.poll()
            time.sleep(.01)
        assert not controller.running and controller.report['status'] == 'cancelled'
        assert not controller.inventory.online
    finally:
        controller.close()
