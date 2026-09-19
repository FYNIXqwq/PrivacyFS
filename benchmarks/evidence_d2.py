"""Synthetic dense D2 graph load. Includes parsing, analysis and public report."""
from pathlib import Path
import argparse
import ctypes
import json
import os
import platform
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from privacyfs.documents import DocumentSession, Limits
from privacyfs.evidence import EvidenceGraph, GraphLimits, public_graph_report
from privacyfs.relations import FieldSpec as F, RecordTemplate as T


def peak_rss():
    if os.name != "nt":
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
            ("peak", "working", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile")]
    data = Memory()
    data.cb = ctypes.sizeof(data)
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    ctypes.windll.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    process = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(data), data.cb):
        raise OSError("RSS unavailable")
    return data.peak


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subjects", type=int, default=500)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--many-files", action="store_true", help="Two files per subject, approximately 50 MiB total; raised retained-character limit")
    args = parser.parse_args()
    if not 1 <= args.subjects <= 2000:
        raise SystemExit("subjects must be 1..2000")
    if args.many_files and args.subjects > 500:
        raise SystemExit("many-file mode supports at most 500 subjects / 1000 files")
    templates = [T((F("cid", "key", "clients"), F("name", "identity")), "cid"),
        T((F("cid", "key", "clients"), F("mid", "key", "meetings", "event")), "cid"),
        T((F("mid", "key", "meetings", "event"), F("status", "sensitive", values=("secret",))), "mid")]
    with tempfile.TemporaryDirectory(prefix="privacyfs-d2-benchmark-") as temp:
        root = Path(temp)
        contents = ["cid,name\n", "cid,mid\n", "mid,status\n"]
        for index in range(args.subjects):
            contents[0] += f"C{index},Synthetic{index}\n"
            contents[1] += f"C{index},M{index}\n"
            contents[2] += f"M{index},secret\n"
        bindings_to_read = []
        if args.many_files:
            templates = [templates[0], T((F("cid", "key", "clients"), F("status", "sensitive", values=("secret",))), "cid")]
            contents = []
            padding = "x" * max(1, 50 * 1024 * 1024 // (args.subjects * 2) - 80)
            for index in range(args.subjects):
                contents.extend([f"cid,name,padding\nC{index},Synthetic{index},{padding}\n",
                                 f"cid,status,padding\nC{index},secret,{padding}\n"])
            bindings_to_read = [(f"{i}.csv", templates[i % 2]) for i in range(len(contents))]
        else:
            bindings_to_read = [(f"{i}.csv", t) for i, t in enumerate(templates)]
        for index, content in enumerate(contents):
            (root / f"{index}.csv").write_bytes(content.encode("utf-8"))
        started = time.perf_counter()
        with DocumentSession(root, limits=Limits(max_seconds=60, max_total_chars=256 * 1024 * 1024,
                                               max_files=max(1000, len(contents)))) as session:
            bindings = [(session.parse(session.capture(path)), t) for path, t in bindings_to_read]
            with EvidenceGraph(bindings, limits=GraphLimits(max_seconds=60, max_witnesses=args.subjects * 3)) as graph:
                result = graph.analyze()
                report = public_graph_report(graph, result)
                assert report["incremental_numbered_joins"] == args.subjects
                output = {"subjects": args.subjects, "files": len(contents), "records": graph.record_count,
                    "input_mib": round(sum(len(s.encode('utf-8')) for s in contents) / 1048576, 2),
                    "retained_character_limit": session.limits.max_total_chars,
                    "witnesses": len(result.witnesses), "evidence": len(result.evidence),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "peak_process_rss_mib": round(peak_rss() / 1048576, 2),
                    "python": platform.python_version(), "platform": platform.platform(),
                    "logical_cpu": os.cpu_count(), "scope": "synthetic_dense_graph_including_parse_and_public_report_no_models_no_export"}
        text = json.dumps(output, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
