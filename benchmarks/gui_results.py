"""Synthetic GUI result storage benchmark; no source scan or model calls.

Run: python benchmarks/gui_results.py
tracemalloc covers Python allocations, not SQLite/DPAPI native memory or RSS.
"""
import json
import platform
import time
import tracemalloc

from privacyfs.gui.results import ResultStore


def sample(index):
    return {"name":f"synthetic-{index}", "path":f"C:/synthetic/{index}",
            "relative_path":str(index), "is_dir":False, "attribution":"self",
            "signals":[{"category":"ORG", "surface":"synthetic", "component":"synthetic",
                        "source":"Mock", "is_self":True}]}


def measure(count):
    tracemalloc.start()
    start = time.perf_counter()
    store = ResultStore()
    try:
        for offset in range(0, count, 128):
            store.extend(sample(i) for i in range(offset, min(count, offset+128)))
        stored_at = time.perf_counter()
        rows, total, pending, _ = store.page((count-1)//80, 80)
        assert total == count and not pending and rows[-1] == sample(count-1)
        page_at = time.perf_counter()
        checked = sum(1 for _ in store)
        assert checked == count
        current, peak = tracemalloc.get_traced_memory()
        disk_bytes = (store.db.execute("PRAGMA page_count").fetchone()[0]
                      * store.db.execute("PRAGMA page_size").fetchone()[0])
        return {"rows":count, "write_seconds":round(stored_at-start, 3),
                "last_page_seconds":round(page_at-stored_at, 3),
                "iterate_seconds":round(time.perf_counter()-page_at, 3),
                "python_peak_mib":round(peak/1024**2, 3),
                "python_current_mib":round(current/1024**2, 3),
                "database_mib":round(disk_bytes/1024**2, 3), "protection":store.provider.name}
    finally:
        store.close()
        tracemalloc.stop()


if __name__ == "__main__":
    print(json.dumps({"python":platform.python_version(), "platform":platform.platform()}, ensure_ascii=True))
    for size in (20_000, 200_000):
        print(json.dumps(measure(size)), flush=True)
