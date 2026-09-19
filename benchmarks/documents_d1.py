"""D1 capture/parse/rules benchmark; no models, no external file inputs."""
from pathlib import Path
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from privacyfs.config import Rules
from privacyfs.documents import DocumentSession, Limits, ParseStatus, RuleInspector
from privacyfs.normalization import NORMALIZATION_VERSION


def peak_rss_mib():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                        ("peak", ctypes.c_size_t), ("working", ctypes.c_size_t),
                        ("pool_peak", ctypes.c_size_t), ("pool", ctypes.c_size_t),
                        ("nonpaged_peak", ctypes.c_size_t), ("nonpaged", ctypes.c_size_t),
                        ("pagefile", ctypes.c_size_t), ("pagefile_peak", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not get_info(current(), ctypes.byref(counters), counters.cb):
            return None
        return round(counters.peak / 1024 / 1024, 1)
    import resource
    units = 1 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * units / 1024 / 1024, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--mib", type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.files <= 1000 or not 1 <= args.mib <= 100:
        parser.error("benchmark accepts 1..1000 files and 1..100 MiB")
    unit = "synthetic document: 普通说明，字段保持原样。 line end\n".encode("utf-8")
    size = args.mib * 1024 * 1024 // args.files
    body = unit * max(1, size // len(unit))
    if len(body) > 8 * 1024 * 1024:
        parser.error("per-file input exceeds default byte limit")
    review = Path(__file__).resolve().parents[1] / ".review"
    review.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="d1-benchmark-", dir=review) as temporary:
        root = Path(temporary)
        for index in range(args.files):
            (root / f"sample-{index}.txt").write_bytes(body)
        started = time.perf_counter()
        inspector = RuleInspector(Rules(use_ner=False))
        count, found, peak_bytes_held = 0, 0, 0
        with DocumentSession(root) as session:
            for index in range(args.files):
                snapshot = session.capture(f"sample-{index}.txt")
                peak_bytes_held = max(peak_bytes_held, session.bytes_held)
                document = session.parse(snapshot)
                if document.coverage.status is not ParseStatus.COMPLETE:
                    raise RuntimeError(document.coverage.reason)
                found += len(inspector.inspect(document))
                count += 1
                session.release(snapshot)
            assert session.bytes_held == 0
        elapsed = time.perf_counter() - started
        print(json.dumps({
            "files": count, "input_mib": round(len(body) * args.files / 1024 / 1024, 2),
            "seconds": round(elapsed, 3), "findings": found,
            "peak_rss_mib": peak_rss_mib(), "peak_snapshot_bytes_held": peak_bytes_held,
            "normalization_version": NORMALIZATION_VERSION,
            "python": sys.version.split()[0], "cpu_count": os.cpu_count(),
            "scope": "Cold capture + literal parsing + default local rules; mixed ASCII/CJK TXT; input creation excluded; RSS is process lifetime peak.",
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
