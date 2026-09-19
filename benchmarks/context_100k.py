"""Synthetic 100k-entry benchmark for the M2 context audit engine.

Run from the repository root:
    py -3.11 benchmarks/context_100k.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from privacyfs.config import Rules
from privacyfs.models import DetectedEntry
from privacyfs.risk import analyze_entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries", type=int, default=100_000)
    parser.add_argument("--subjects", type=int, default=100)
    args = parser.parse_args()
    if args.entries < 1 or args.subjects < 1:
        parser.error("--entries and --subjects must be positive")

    entries = [
        DetectedEntry(
            entry_id=f"ENTRY_{index:024x}",
            raw_path=Path(
                f"subject-{index % args.subjects}/北京大学/计算机专业/"
                f"2024-{index % 12 + 1:02d}-记录-{index}.txt"
            ),
            is_dir=False,
            depth=4,
        )
        for index in range(args.entries)
    ]
    rules = Rules(ner_engine="off", context_k=3)
    rules.use_ner = False

    tracemalloc.start()
    started = time.perf_counter()
    result = analyze_entries(entries, rules, namespace="benchmark")
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(json.dumps({
        "entries": args.entries,
        "subjects": result.subjects,
        "risks": len(result.risks),
        "seconds": round(elapsed, 3),
        "entries_per_second": round(args.entries / elapsed),
        "peak_mib": round(peak / 1024 / 1024, 1),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
