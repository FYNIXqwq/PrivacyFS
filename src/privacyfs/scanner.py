"""Directory walking, serial and thread-parallel."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


@dataclass
class RawEntry:
    relpath: Path      # relative to scan root
    is_dir: bool
    depth: int
    size: int          # 0 for dirs
    hidden: bool = False  # dir matched a hidden keyword: not descended


def winlong(path: str | Path) -> str:
    """Add the \\\\?\\ prefix on Windows when a path nears the 260-char limit."""
    s = str(path)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        s = os.path.abspath(s)
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s[2:]
        return "\\\\?\\" + s
    return s


def norm_path(path: str | Path) -> str:
    """Canonical form for path-identity comparison (case-insensitive on Windows)."""
    s = os.path.normpath(str(path))
    if s.startswith("\\\\?\\UNC\\"):
        s = "\\\\" + s[len("\\\\?\\UNC\\"):]
    elif s.startswith("\\\\?\\"):
        s = s[len("\\\\?\\"):]
    return os.path.normcase(s)


PRIVATE_STATE_MARKER = ".privacyfs-state.json"


def has_private_state_marker(path: Path) -> bool:
    # Presence is a reserved exclusion boundary, even if malformed. Do not open
    # the marker: it contains a state authentication key.
    return os.path.lexists(winlong(Path(path) / PRIVATE_STATE_MARKER))


def inside_private_state(path: Path) -> bool:
    path = Path(os.path.abspath(path))
    return any(has_private_state_marker(p) for p in (path, *path.parents))


def _is_reparse(child: os.DirEntry) -> bool:
    """Symlink or junction. DirEntry.is_junction exists only on 3.12+;
    older versions fall back to the reparse-point file attribute."""
    if child.is_symlink():
        return True
    if hasattr(child, "is_junction"):
        return child.is_junction()
    attrs = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def dir_size(path: Path, cancel=None) -> int:
    """Size-only deep sum of a subtree (no detection, no symlink following).
    Used for [HIDDEN] dirs whose children are never walked by the pipeline."""
    total = 0
    if inside_private_state(path):
        return total
    stack = [winlong(path)]
    while stack:
        if cancel is not None and cancel.is_set():
            return total
        dirpath = stack.pop()
        try:
            children = list(os.scandir(dirpath))
        except OSError:
            continue
        for child in children:
            try:
                if _is_reparse(child):
                    continue
                if child.is_dir(follow_symlinks=False):
                    if not has_private_state_marker(Path(child.path)):
                        stack.append(child.path)
                else:
                    total += child.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _compile_excludes(excludes: list[str]) -> re.Pattern | None:
    """One regex for all fnmatch patterns. fnmatch.fnmatch() re-translates and
    re-normcases the name on EVERY call -- that's the per-entry hot spot."""
    if not excludes:
        return None
    parts = [fnmatch.translate(p) for p in excludes]
    # strip the trailing \\Z\\s* style anchors and re-anchor the alternation
    bodies = [p[: -2] if p.endswith("\\Z") else p for p in parts]
    bodies = [b[:-4] if b.endswith("\\s*") else b for b in bodies]
    bodies = [b.rstrip("$") for b in bodies]
    return re.compile("(?:" + "|".join("(?:" + b + ")" for b in bodies) + ")$",
                      re.IGNORECASE)


def _scan_one_dir(dirpath: str, relpath: Path, depth: int,
                  ex_re: re.Pattern | None, is_hidden_dir, exclude_paths,
                  with_size: bool, max_level):
    """Scan one directory; returns (entries, subdirs-to-descend).

    Everything hot is precomputed by the caller: excludes is one compiled
    regex, and exclude_paths should be EMPTY unless a protected file is
    actually inside this tree (checking norm_path per entry is expensive).
    """
    entries: list[RawEntry] = []
    subdirs: list[tuple[str, Path, int]] = []
    if has_private_state_marker(Path(dirpath)):
        return entries, subdirs
    try:
        children = list(os.scandir(dirpath))
    except OSError:
        return entries, subdirs
    for child in children:
        name = child.name
        if ex_re is not None and ex_re.match(name):
            continue
        if exclude_paths and norm_path(child.path) in exclude_paths:
            continue
        child_rel = relpath / name if str(relpath) else Path(name)
        try:
            if _is_reparse(child):
                continue
            if child.is_dir(follow_symlinks=False):
                if has_private_state_marker(Path(child.path)):
                    continue
                hidden = is_hidden_dir(name)
                entries.append(RawEntry(child_rel, True, depth, 0, hidden))
                if not hidden and (max_level is None or len(child_rel.parts) < max_level):
                    subdirs.append((child.path, child_rel, depth + 1))
            else:
                size = 0
                if with_size:
                    try:
                        size = child.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
                entries.append(RawEntry(child_rel, False, depth, size))
        except OSError:
            continue
    return entries, subdirs


def walk(
    root: Path,
    excludes: list[str],
    is_hidden_dir: Callable[[str], bool],
    exclude_paths: frozenset[str] = frozenset(),
    with_size: bool = True,
    max_level: int | None = None,
    progress: Callable[[int], None] | None = None,
    cancel=None,
) -> Iterator[RawEntry]:
    """Serial fallback / small trees. See walk_parallel for the normal path."""
    if inside_private_state(root):
        return
    ex_re = _compile_excludes(excludes)
    stack: list[tuple[str, Path, int]] = [(winlong(root), Path(""), 0)]
    n = 0
    while stack:
        if cancel is not None and cancel.is_set():
            return
        dirpath, relpath, depth = stack.pop()
        entries, subdirs = _scan_one_dir(dirpath, relpath, depth, ex_re,
                                         is_hidden_dir, exclude_paths, with_size,
                                         max_level)
        for e in entries:
            n += 1
            if progress is not None and n % 5000 == 0:
                progress(n)
            yield e
        stack.extend(subdirs)


def walk_parallel(
    root: Path,
    excludes: list[str],
    is_hidden_dir: Callable[[str], bool],
    exclude_paths: frozenset[str] = frozenset(),
    with_size: bool = True,
    max_level: int | None = None,
    progress: Callable[[int], None] | None = None,
    workers: int = 16,
    cancel=None,
) -> Iterator[RawEntry]:
    """BFS with a thread pool: many directories' scandir/stat calls are in
    flight at once, so disk IO actually parallelizes. The Python-side work
    per entry is kept minimal (precompiled excludes, no per-entry normpath)
    so the GIL doesn't become the new bottleneck. Output order is
    nondeterministic; callers sort. `cancel` (threading.Event) stops the
    walk promptly and drops pending tasks without joining them."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    if inside_private_state(root):
        return

    ex_re = _compile_excludes(excludes)
    n = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        pending = {ex.submit(_scan_one_dir, winlong(root), Path(""), 0,
                             ex_re, is_hidden_dir, exclude_paths, with_size,
                             max_level)}
        while pending:
            if cancel is not None and cancel.is_set():
                return
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                entries, subdirs = fut.result()
                for e in entries:
                    n += 1
                    if progress is not None and n % 5000 == 0:
                        progress(n)
                    yield e
                for path, relpath, depth in subdirs:
                    pending.add(ex.submit(_scan_one_dir, path, relpath, depth,
                                          ex_re, is_hidden_dir, exclude_paths,
                                          with_size, max_level))
    finally:
        if cancel is not None and cancel.is_set():
            # do NOT join pending scans; their whole point is to die now
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            ex.shutdown(wait=True)
