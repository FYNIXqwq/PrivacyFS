"""One deterministic target namespace shared by listings and mirrors."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path, PureWindowsPath

from .models import MappedEntry

MIRROR_MARKER = ".privacyfs-mirror"


def relative_parts(value: str | Path) -> tuple[str, ...]:
    """Reject traversal, rooted/drive paths and Windows alternate streams."""
    text = str(value).replace("\\", "/")
    parts = tuple(text.split("/"))
    if (not text or PureWindowsPath(text).drive or text.startswith("/")
            or any(p in {"", ".", ".."} for p in parts)
            or any(":" in p or "\x00" in p for p in parts)):
        raise ValueError("invalid relative path in mapping")
    if os.name == "nt" and any(
        p.endswith((".", " ")) or PureWindowsPath(p).is_reserved()
        or any(ord(ch) < 32 or ch in '<>"|?*' for ch in p)
        for p in parts
    ):
        raise ValueError("unsupported Windows filename in mapping")
    return parts


def path_key(value: str) -> str:
    return value.casefold() if os.name == "nt" else value


def validate_targets(entries: list[MappedEntry]) -> None:
    """Validate the entire namespace before a mirror creates any files."""
    occupied: dict[str, bool] = {path_key(MIRROR_MARKER): False}
    for entry in entries:
        relative_parts(entry.raw)
        parts = relative_parts(entry.clean)
        key = path_key("/".join(parts))
        if key in occupied:
            raise ValueError("duplicate or reserved target in mapping")
        occupied[key] = entry.is_dir
    for entry in entries:
        parts = relative_parts(entry.clean)
        for i in range(1, len(parts)):
            if occupied.get(path_key("/".join(parts[:i]))) is False:
                raise ValueError("file used as a directory in mapping")


def plan_paths(entries: list[MappedEntry]) -> list[MappedEntry]:
    """Allocate collisions once, rewriting descendants through source parents.

    The ordinary pipeline includes visible ancestor directories. If a caller
    supplies inconsistent transformed ancestry, reject instead of merging data.
    """
    occupied: set[str] = {path_key(MIRROR_MARKER)}
    directories: dict[tuple[str, ...], tuple[str, str]] = {}
    seen_raw: set[tuple[str, ...]] = set()
    result: list[MappedEntry] = []
    ordered = sorted(entries, key=lambda e: (
        len(e.raw.parts), not e.is_dir, e.raw.as_posix(), e.clean,
    ))
    for entry in ordered:
        raw = relative_parts(entry.raw)
        if raw in seen_raw:
            raise ValueError("duplicate source in mapping")
        seen_raw.add(raw)
        desired = "/".join(relative_parts(entry.clean))
        target = desired
        for level in range(len(raw) - 1, 0, -1):
            parent = directories.get(raw[:level])
            if parent is not None:
                original, planned = parent
                if not desired.startswith(original + "/"):
                    raise ValueError("inconsistent directory mapping")
                target = planned + desired[len(original):]
                break
        base = target
        ordinal = 2
        while path_key(target) in occupied:
            parent, _, leaf = base.rpartition("/")
            stem, suffix = os.path.splitext(leaf) if not entry.is_dir else (leaf, "")
            leaf = f"{stem}__{ordinal}{suffix}"
            target = f"{parent}/{leaf}" if parent else leaf
            ordinal += 1
        occupied.add(path_key(target))
        if entry.is_dir:
            directories[raw] = (desired, target)
        result.append(replace(entry, clean=target,
                              collision=entry.collision or target != base))
    validate_targets(result)
    return sorted(result, key=lambda entry: entry.clean)
