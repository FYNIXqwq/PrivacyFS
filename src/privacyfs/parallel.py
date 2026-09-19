"""Process-pool detection workers.

Spawn-safe by construction: nothing is built at import time; each worker
rebuilds its detectors from the (picklable) Rules in the initializer. The
pools are only ever created inside iter_mapped(), never at import, so the
console-script entry point is safe under Windows spawn.
"""

from __future__ import annotations

from .config import Rules

_DETS = None


def init_worker(rules: Rules) -> None:
    """Per-process: build the plain (non-batch, non-late) detectors."""
    global _DETS
    from .core import _detectors_for

    _DETS = [d for d in _detectors_for(rules, pseudo=None)
             if not hasattr(d, "find_batch") and not getattr(d, "late", False)]


def detect_chunk(parts: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Returns {part: [(category, surface), ...]} for parts with findings."""
    out: dict[str, list[tuple[str, str]]] = {}
    for p in parts:
        findings = [(f.category, f.surface) for det in _DETS for f in det.find(p)]
        if findings:
            out[p] = findings
    return out
