"""Scan pipeline: walk -> detect -> pseudonymize -> sanitized entries."""

from __future__ import annotations

from pathlib import Path
import re
import threading
from typing import Callable

from .config import Rules, expand_terms
from .detectors.ennames import EnNameDetector
from .detectors.keywords import Finding, KeywordDetector, RegexDetector
from .detectors.names import NameNER, SurnameHeuristic
from .detectors.orgs import OrgDetector
from .models import (
    DetectedEntry,
    DetectedSignal,
    MappedEntry,
    SanitizedEntry,
    ScanResult,
    make_entry_id,
)
from .pseudonym import Pseudonymizer
from .paths import plan_paths
from .source_map import SpanEdit, apply_edits
from .scanner import RawEntry, dir_size, walk, walk_parallel

HIDDEN_LABEL = "[HIDDEN]"


class ScanCancelled(Exception):
    """Raised when the user aborts a scan (TUI quit etc.)."""


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled()


# The active detection pool, so a TUI quit can actually kill worker
# processes instead of hanging on multiprocessing's atexit join.
_ACTIVE_POOL = None
_POOL_LOCK = threading.Lock()


def _register_pool(pool) -> None:
    global _ACTIVE_POOL
    with _POOL_LOCK:
        _ACTIVE_POOL = pool


def _stop_pool(pool) -> None:
    """Capture workers before shutdown clears executor internals."""
    with _POOL_LOCK:
        if getattr(pool, "_privacyfs_stopped", False):
            return
        pool._privacyfs_stopped = True
        processes = list((getattr(pool, "_processes", None) or {}).values())
    for process in processes:
        try:
            process.terminate()
        except (OSError, ValueError):
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    finally:
        for process in processes:
            try:
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
            except (OSError, ValueError):
                pass


def kill_active_pool() -> None:
    """Terminate any in-flight detection workers. Called on TUI quit."""
    global _ACTIVE_POOL
    with _POOL_LOCK:
        pool, _ACTIVE_POOL = _ACTIVE_POOL, None
    if pool is None:
        return
    _stop_pool(pool)


def _parallel_findings(parts, rules, jobs, progress, cancel):
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
    import multiprocessing as mp
    from .parallel import detect_chunk, init_worker

    pool = ProcessPoolExecutor(max_workers=jobs, mp_context=mp.get_context("spawn"),
                               initializer=init_worker, initargs=(rules,))
    _register_pool(pool)
    output = {}
    try:
        pending = set()
        for start in range(0, len(parts), 500):
            _check_cancel(cancel)
            pending.add(pool.submit(detect_chunk, parts[start:start + 500]))
        total, completed = len(pending), 0
        while pending:
            _check_cancel(cancel)
            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                _check_cancel(cancel)
                for part, pairs in future.result().items():
                    output[part] = [(Finding(category, surface), "ProcessPoolDetector")
                                    for category, surface in pairs]
                completed += 1
                if progress is not None:
                    progress(f"并行检测中... {completed}/{total} 批")
        _check_cancel(cancel)
        return output
    except BaseException:
        _stop_pool(pool)
        _check_cancel(cancel)  # translate worker termination during TUI quit
        raise
    finally:
        _register_pool(None)
        pool.shutdown(wait=True, cancel_futures=True)


def _detectors_for(rules: Rules, pseudo: Pseudonymizer | None = None):
    from .detectors import hanlp_ner

    terms = [(kw, "KEYWORD") for kw in expand_terms(rules.keywords)]
    org_suffixes = tuple(expand_terms(rules.org_suffixes))
    explicit_orgs = set(expand_terms(rules.literal_orgs))
    terms += [(n, "ORG" if n in explicit_orgs or (org_suffixes and n.endswith(org_suffixes))
               else "NAME_LIST") for n in expand_terms(rules.literal_names)]
    terms += [(n, "ORG") for n in explicit_orgs]
    if rules.detect_professions:
        terms += [(p, "PROFESSION") for p in expand_terms(rules.professions)]
    if rules.detect_identity:
        terms += [(t, "IDENTITY") for t in expand_terms(rules.identity_terms)]
    dets = [KeywordDetector(terms), RegexDetector(rules.compiled_regexes())]
    if rules.detect_orgs:
        dets.append(OrgDetector(expand_terms(rules.org_suffixes)))
    engine = rules.ner_engine if rules.use_ner else "off"
    if engine == "auto":
        # union: HanLP catches rare surnames, jieba catches common names
        # the transformer model misses on context-free strings
        if hanlp_ner.available():
            dets.append(hanlp_ner.HanlpNER())
        dets.append(NameNER())
    elif engine == "hanlp":
        if hanlp_ner.available():
            dets.append(hanlp_ner.HanlpNER())
        else:
            dets.append(NameNER())  # hanlp requested but not installed
    elif engine == "jieba":
        dets.append(NameNER())
    dets.append(SurnameHeuristic())
    if rules.detect_en_names:
        dets.append(EnNameDetector())
    if rules.detect_pinyin:
        from .detectors.pinyin import PinyinDetector

        dets.append(PinyinDetector(rules.pinyin_allowlist))
    if rules.use_llm:
        from .detectors.llm import LLMDetector

        dets.append(LLMDetector(model=rules.llm_model, url=rules.llm_url,
                                cache=pseudo, max_items=rules.llm_max, revision=rules.llm_revision))
    return dets


def sanitize_path(
    relpath: Path,
    findings: list[Finding],
    pseudo: Pseudonymizer,
) -> tuple[str, list[str]]:
    """Replace original, leftmost-longest non-overlapping spans in one pass."""
    text = relpath.as_posix()
    categories: dict[str, str] = {}
    for finding in findings:
        if finding.surface:
            categories.setdefault(finding.surface, finding.category)
    if not categories:
        return text, []
    pattern = re.compile("|".join(re.escape(s) for s in sorted(categories, key=len, reverse=True)))
    used: set[str] = set()
    edits = []
    for match in pattern.finditer(text):
        surface = match.group(0)
        alias = pseudo.alias(categories[surface], surface)
        used.add(alias)
        edits.append(SpanEdit(match.start(), match.end(), alias))
    return apply_edits(text, edits), sorted(used)


def _resolve_jobs(jobs: int) -> int:
    if jobs > 0:
        return jobs
    import os

    return os.cpu_count() or 4


def _aggregate_dir_sizes(
    root: Path,
    raws: list[RawEntry],
    with_size: bool,
    cancel=None,
) -> dict[str, int]:
    """Aggregate recursive sizes without exposing names outside the pipeline."""
    if not with_size:
        return {}

    # String operations here avoid constructing one Path per ancestor per file.
    dir_sizes: dict[str, int] = {}
    for raw in raws:
        _check_cancel(cancel)
        if raw.is_dir:
            continue
        rel = raw.relpath.as_posix()
        start = 0
        while True:
            idx = rel.find("/", start)
            if idx == -1:
                break
            key = rel[:idx]
            dir_sizes[key] = dir_sizes.get(key, 0) + raw.size
            start = idx + 1

    # Hidden subtrees are never walked. Sum them separately and propagate the
    # total into every visible ancestor.
    for raw in raws:
        if not raw.is_dir or not raw.hidden:
            continue
        sub = dir_size(root / raw.relpath, cancel=cancel)
        _check_cancel(cancel)
        rel = raw.relpath.as_posix()
        dir_sizes[rel] = sub
        start = 0
        while True:
            idx = rel.find("/", start)
            if idx == -1:
                break
            ancestor = rel[:idx]
            dir_sizes[ancestor] = dir_sizes.get(ancestor, 0) + sub
            start = idx + 1
    return dir_sizes


def walk_entries(
    root: Path,
    rules: Rules,
    exclude_paths: frozenset[str] = frozenset(),
    progress: Callable[[str], None] | None = None,
    with_size: bool = True,
    max_level: int | None = None,
    jobs: int = 1,
    cancel=None,
) -> tuple[list[RawEntry], dict[str, int]]:
    """Walk the filesystem and return raw entries plus recursive dir sizes.

    This is the only pipeline stage that enumerates the real filesystem.
    ``max_level`` limits the walk only when sizes are disabled; recursive size
    aggregation otherwise requires walking all visible descendants.
    """
    jobs = _resolve_jobs(jobs)
    hidden_lower = [k.lower() for k in expand_terms(rules.hidden_keywords)]

    def is_hidden_dir(name: str) -> bool:
        low = name.lower()
        return any(keyword in low for keyword in hidden_lower)

    walk_level = max_level if not with_size else None
    walk_progress = (lambda n: progress(f"遍历目录... {n} 条")) if progress else None
    if jobs > 1:
        raws = list(walk_parallel(
            root,
            rules.excludes,
            is_hidden_dir,
            exclude_paths,
            with_size,
            walk_level,
            walk_progress,
            workers=max(8, jobs * 2),
            cancel=cancel,
        ))
    else:
        raws = list(walk(
            root,
            rules.excludes,
            is_hidden_dir,
            exclude_paths,
            with_size,
            walk_level,
            walk_progress,
            cancel,
        ))
    _check_cancel(cancel)
    if progress is not None:
        progress(f"遍历完成 {len(raws)} 条，检测中...")
    sizes = _aggregate_dir_sizes(root, raws, with_size, cancel)
    _check_cancel(cancel)
    return raws, sizes


def _component_findings(
    parts: list[str],
    rules: Rules,
    pseudo: Pseudonymizer | None,
    jobs: int,
    progress: Callable[[str], None] | None,
    cancel,
) -> dict[str, list[tuple[Finding, str]]]:
    """Run all detectors once per unique path component."""
    detectors = _detectors_for(rules, pseudo)
    batch_dets = [
        detector for detector in detectors
        if hasattr(detector, "find_batch") and not getattr(detector, "late", False)
    ]
    plain_dets = [
        detector for detector in detectors
        if not hasattr(detector, "find_batch") and not getattr(detector, "late", False)
    ]
    late_dets = [detector for detector in detectors if getattr(detector, "late", False)]

    batch_cache: dict[str, list[tuple[Finding, str]]] = {}
    plain_cache: dict[str, list[tuple[Finding, str]]] = {}

    for detector in batch_dets:
        _check_cancel(cancel)
        source = type(detector).__name__
        if progress is not None:
            progress(f"{source} 检测中（{len(parts)} 个唯一名称）...")
        for part, findings in detector.find_batch(parts).items():
            batch_cache.setdefault(part, []).extend(
                (finding, source) for finding in findings
            )
        _check_cancel(cancel)

    # CPU-bound plain detectors use processes only at a scale where spawn and
    # serialization overhead are worthwhile.
    if plain_dets and jobs > 1 and len(parts) >= 20000:
        plain_cache = _parallel_findings(parts, rules, jobs, progress, cancel)
        for part in parts:
            plain_cache.setdefault(part, [])

    def findings_for(part: str) -> list[tuple[Finding, str]]:
        _check_cancel(cancel)
        plain = plain_cache.get(part)
        if plain is None:
            plain = []
            for detector in plain_dets:
                source = type(detector).__name__
                plain.extend((finding, source) for finding in detector.find(part))
            plain_cache[part] = plain
        return plain + batch_cache.get(part, [])

    if late_dets:
        unmatched = [part for part in parts if not findings_for(part)]
        for detector in late_dets:
            if progress is not None and hasattr(detector, "progress"):
                detector.progress = lambda message: progress(f"LLM 判定中 {message}")
            if hasattr(detector, "cancel"):
                detector.cancel = cancel
            source = type(detector).__name__
            for part, findings in detector.find_batch(unmatched).items():
                batch_cache.setdefault(part, []).extend(
                    (finding, source) for finding in findings
                )
            _check_cancel(cancel)

    return {part: findings_for(part) for part in parts}


def detect_entries(
    raws: list[RawEntry],
    rules: Rules,
    pseudo: Pseudonymizer | None = None,
    dir_sizes: dict[str, int] | None = None,
    progress: Callable[[str], None] | None = None,
    max_level: int | None = None,
    jobs: int = 1,
    cancel=None,
    entry_namespace: str = "scan",
) -> list[DetectedEntry]:
    """Detect sensitive signals without pseudonymizing or serializing paths."""
    jobs = _resolve_jobs(jobs)
    dir_sizes = dir_sizes or {}
    listed = [
        raw for raw in raws
        if max_level is None or len(raw.relpath.parts) <= max_level
    ]
    parts = sorted({part for raw in listed for part in raw.relpath.parts})
    findings_by_part = _component_findings(
        parts, rules, pseudo, jobs, progress, cancel
    )

    detected: list[DetectedEntry] = []
    for index, raw in enumerate(listed):
        if index % 2000 == 0:
            _check_cancel(cancel)
        signals: list[DetectedSignal] = []
        for component_index, part in enumerate(raw.relpath.parts):
            for finding, source in findings_by_part.get(part, []):
                signals.append(DetectedSignal.from_finding(
                    finding,
                    component_index=component_index,
                    source=source,
                ))
        size = raw.size if not raw.is_dir else dir_sizes.get(raw.relpath.as_posix(), 0)
        detected.append(DetectedEntry(
            entry_id=make_entry_id(raw.relpath, entry_namespace),
            raw_path=raw.relpath,
            is_dir=raw.is_dir,
            size=size,
            depth=raw.depth,
            hidden=raw.hidden,
            signals=signals,
        ))
    return detected


def transform_entries(
    detected: list[DetectedEntry],
    pseudo: Pseudonymizer,
    progress: Callable[[str], None] | None = None,
    cancel=None,
) -> list[MappedEntry]:
    """Apply pseudonyms to detected entries while retaining mirror internals."""
    mapped: list[MappedEntry] = []
    for index, entry in enumerate(detected):
        if index % 2000 == 0:
            _check_cancel(cancel)
        release_path = entry.display_path if entry.display_path is not None else entry.raw_path
        clean_parts = []
        used_aliases: set[str] = set()
        for component_index, part in enumerate(release_path.parts):
            findings = [Finding(signal.category, signal.surface) for signal in entry.signals
                        if signal.component_index == component_index]
            clean_part, part_aliases = sanitize_path(Path(part), findings, pseudo)
            clean_parts.append(clean_part)
            used_aliases.update(part_aliases)
        clean, aliases = "/".join(clean_parts), sorted(used_aliases)
        if entry.hidden and entry.display_path is None:
            clean = (
                str(Path(clean).parent / HIDDEN_LABEL).replace("\\", "/")
                if len(entry.raw_path.parts) > 1
                else HIDDEN_LABEL
            )
        mapped.append(MappedEntry(
            raw=entry.raw_path,
            clean=clean,
            is_dir=entry.is_dir,
            size=entry.size,
            aliases=aliases,
            hidden=entry.hidden,
            entry_id=entry.entry_id,
        ))
        if progress is not None and index % 2000 == 1999:
            progress(f"生成化名... {index + 1}/{len(detected)}")

    return plan_paths(mapped)


def iter_mapped(
    root: Path,
    rules: Rules,
    pseudo: Pseudonymizer,
    exclude_paths: frozenset[str] = frozenset(),
    progress: Callable[[str], None] | None = None,
    with_size: bool = True,
    max_level: int | None = None,
    jobs: int = 1,
    cancel=None,
) -> list[MappedEntry]:
    """Compatibility orchestrator: walk, detect, then pseudonymize."""
    from .scanner import norm_path
    root = root.resolve()
    if pseudo.db_path.is_relative_to(root):
        exclude_paths = exclude_paths | frozenset(
            norm_path(str(pseudo.db_path) + suffix)
            for suffix in ("", "-journal", "-wal", "-shm")
        )
    raws, dir_sizes = walk_entries(
        root,
        rules,
        exclude_paths=exclude_paths,
        progress=progress,
        with_size=with_size,
        max_level=max_level,
        jobs=jobs,
        cancel=cancel,
    )
    detected = detect_entries(
        raws,
        rules,
        pseudo=pseudo,
        dir_sizes=dir_sizes,
        progress=progress,
        max_level=max_level,
        jobs=jobs,
        cancel=cancel,
    )
    return transform_entries(detected, pseudo, progress=progress, cancel=cancel)


def build_scan_result(
    root: Path,
    mapped: list[MappedEntry],
    rules: Rules,
    pseudo: Pseudonymizer,
    *,
    root_label: str | None = None,
) -> ScanResult:
    """Build the public result boundary from internal mapped entries."""
    files = sum(1 for entry in mapped if not entry.is_dir)
    dirs = sum(1 for entry in mapped if entry.is_dir)
    hidden_dirs = sum(1 for entry in mapped if entry.hidden)
    entries = [
        SanitizedEntry(
            path=entry.clean,
            is_dir=entry.is_dir,
            size=entry.size,
            aliases=entry.aliases,
            hidden=entry.hidden,
            entry_id=entry.entry_id,
        )
        for entry in mapped
    ]

    # The root's own name may itself be sensitive (for example C:\Users\张三).
    if root_label is None:
        detectors = _detectors_for(rules, pseudo)
        root_findings = [finding for detector in detectors for finding in detector.find(root.name)]
        root_label, _ = sanitize_path(Path(root.name or str(root)), root_findings, pseudo)
    return ScanResult(root_label, entries, files, dirs, hidden_dirs)


def to_result(
    root: Path,
    mapped: list[MappedEntry],
    rules: Rules,
    pseudo: Pseudonymizer,
) -> ScanResult:
    """Backward-compatible alias for build_scan_result()."""
    return build_scan_result(root, mapped, rules, pseudo)


def scan(
    root: Path,
    rules: Rules,
    pseudo: Pseudonymizer,
    exclude_paths: frozenset[str] = frozenset(),
    max_level: int | None = None,
) -> ScanResult:
    mapped = iter_mapped(root, rules, pseudo, exclude_paths, max_level=max_level)
    return build_scan_result(root, mapped, rules, pseudo)
