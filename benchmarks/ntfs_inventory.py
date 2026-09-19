"""Synthetic metadata/index workload, NOT a real MFT-vs-walk speed claim."""
import argparse
import json
import itertools
from pathlib import Path
import platform
import tempfile
import threading
import time
import tracemalloc

from privacyfs.ntfs.index import build_inventory
from privacyfs.ntfs.preview import build_preview
from privacyfs.ntfs.native import Record,Journal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files",type=int,default=100000)
    parser.add_argument("--preliminary", action="store_true")
    parser.add_argument("--no-tracemalloc", action="store_true")
    parser.add_argument("--ui-store", action="store_true")
    parser.add_argument("--disk-ui-store", action="store_true")
    parser.add_argument("--disk-preview", action="store_true")
    parser.add_argument("--directories", type=int, default=1000)
    parser.add_argument("--interleaved", action="store_true")
    parser.add_argument("--nested", action="store_true")
    parser.add_argument("--lazy-preview", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.directories < 999000:
        parser.error("directories must be between 1 and 998999")
    class Source:
        root_id = 5
        def __init__(self,root):
            pass
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def journal(self):
            return Journal(1,0,100)
        def changes(self,*args):
            return iter(())
        def records(self,cancel):
            def parent(i):
                return 5 if not args.nested or i == 0 else 1000+((i*1103515245+12345)&0x7fffffff)%i
            if args.interleaved:
                for i in range(max(args.files,args.directories)):
                    if i < args.directories:
                        yield Record(1000+i,parent(i),0x10,0,f"folder-{i}")
                    if i < args.files:
                        yield Record(1000000+i,1000+i%args.directories,0,0,f"file-{i}.txt")
                return
            for i in range(args.directories):
                yield Record(1000+i,parent(i),0x10,0,f"folder-{i}")
            for i in range(args.files):
                yield Record(1000000+i,1000+i%args.directories,0,0,f"file-{i}.txt")
        def gate_directory(self,*args):
            return "ok"
        def file_links(self,identifier):
            i = identifier-1000000
            return 0,[(1000+i%args.directories,f"file-{i}.txt")]
    root = Path(tempfile.mkdtemp(prefix="privacyfs-ntfs-benchmark-"))
    inventory = None
    try:
        if not args.no_tracemalloc:
            tracemalloc.start()
        started = time.perf_counter()
        builder = build_preview if args.preliminary else build_inventory
        phases = {}
        def progress(**values):
            phases.setdefault(values["phase"], time.perf_counter()-started)
        if args.lazy_preview:
            inventory = build_inventory(root,[],threading.Event(),progress,source_factory=Source,preliminary=True,lazy=True)
        elif args.disk_preview:
            inventory = build_inventory(root,[],threading.Event(),progress,source_factory=Source,preliminary=True)
        else:
            inventory = builder(root,[],threading.Event(),progress,source_factory=Source)
        built = time.perf_counter()
        first_page_seconds = child_page_seconds = None
        if args.lazy_preview:
            from privacyfs.ntfs.browser import browse
            def read_page(parent='.', directory_id=None):
                rows, cursor = [], 0
                while len(rows) < 81:
                    reply = browse(inventory,parent,after=cursor,limit=81-len(rows),directory_id=directory_id)
                    rows.extend(reply['rows'])
                    cursor = reply['cursor']
                    if not reply['pending']:
                        break
                return {'rows':rows}
            page = read_page()
            first_page_seconds = time.perf_counter()-built
            count = len(page['rows'])
            child = next(r for r in page['rows'] if r['is_dir'])
            child_start = time.perf_counter()
            read_page(child['relative_path'], directory_id=child['_directory_id'])
            child_page_seconds = time.perf_counter()-child_start
            assert inventory.db.execute('SELECT count(*) FROM allowed').fetchone()[0] == 0
        elif args.ui_store:
            from privacyfs.workbench.tree_store import PreviewTreeStore, TreeStore
            store = TreeStore() if args.disk_ui_store else PreviewTreeStore()
            try:
                rows = inventory.rows() if hasattr(inventory,"rows") else (
                    {"relative_path":p.as_posix(),"is_dir":d,"name":p.name} for p,d,_ in inventory.entries())
                numbered = ({**row,"entry_id":f"E{i}"} for i,row in enumerate(rows,1))
                while chunk := list(itertools.islice(numbered,512)):
                    store.extend(chunk)
                count = len(store)
            finally:
                store.close()
        else:
            count = sum(1 for _ in inventory.entries())
        ended = time.perf_counter()
        _, peak = tracemalloc.get_traced_memory()
        if not args.lazy_preview:
            assert count == args.files+args.directories
        print(json.dumps({"python":platform.python_version(),"native_io":False,"preliminary":args.preliminary or args.disk_preview or args.lazy_preview,"files":args.files,"directories":args.directories,
            "disk_preview":args.disk_preview,"interleaved":args.interleaved,"nested":args.nested,"phase_start_seconds":{k:round(v,3) for k,v in phases.items()},
            "lazy_preview":args.lazy_preview,"first_page_seconds":first_page_seconds,"child_page_seconds":child_page_seconds,
            "entries":count,"build_seconds":round(built-started,3),"iterate_seconds":round(ended-built,3),
            "tracemalloc":not args.no_tracemalloc,
            "ui_store":args.ui_store,"disk_ui_store":args.disk_ui_store,"ipc_and_render":False,
            "python_peak_mib":None if args.no_tracemalloc else round(peak/1024**2,2),
            "index_mib":round(inventory.database_path.stat().st_size/1024**2,2) if hasattr(inventory,"database_path") else 0}))
    finally:
        if inventory:
            inventory.close()
        tracemalloc.stop()
        root.rmdir()  # Only the freshly created, empty synthetic root.


if __name__ == "__main__":
    main()
