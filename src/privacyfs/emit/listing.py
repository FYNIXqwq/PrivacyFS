"""Output renderers. Input is always SanitizedEntry -- real names never
reach this layer."""

from __future__ import annotations

import json
from collections import defaultdict

from rich.cells import cell_len

from ..models import ScanResult


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def to_json(result: ScanResult, include_size: bool = True) -> str:
    payload = {
        "root": result.root_label,
        "stats": {
            "files": result.files,
            "dirs": result.dirs,
            "hidden_dirs": result.hidden_dirs,
        },
        "entries": [
            {
                "path": e.path,
                "type": "dir" if e.is_dir else "file",
                **({"size": e.size_label or e.size} if include_size else {}),
                **({"hidden": True} if e.hidden else {}),
            }
            for e in result.entries
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _entry_json(e, include_size: bool) -> str:
    d: dict = {"path": e.path, "type": "dir" if e.is_dir else "file"}
    if include_size:
        # Enforced context views carry bucketed labels; ordinary scans retain
        # the historical exact integer sizes.
        d["size"] = e.size_label or e.size
    if e.hidden:
        d["hidden"] = True
    return json.dumps(d, ensure_ascii=False)


def write_json(result: ScanResult, out, include_size: bool = True) -> None:
    """Stream JSON to a text stream. to_json() builds one giant in-memory
    string, which hurts at millions of entries; this writes incrementally."""
    out.write("{\n")
    out.write(f'  "root": {json.dumps(result.root_label, ensure_ascii=False)},\n')
    stats = {"files": result.files, "dirs": result.dirs, "hidden_dirs": result.hidden_dirs}
    out.write(f'  "stats": {json.dumps(stats, ensure_ascii=False)},\n')
    out.write('  "entries": [\n')
    n = len(result.entries)
    for i, e in enumerate(result.entries):
        out.write("    " + _entry_json(e, include_size) + (",\n" if i < n - 1 else "\n"))
    out.write("  ]\n}\n")


def to_tree_lines(result: ScanResult, show_size: bool = True) -> list[tuple[str, str]]:
    """(text, size) pairs forming an aligned tree. Plain text; callers print
    with markup disabled since sanitized paths may contain '[' etc."""
    lines: list[tuple[str, str]] = []
    total = sum(e.size for e in result.entries if "/" not in e.path)
    if show_size and any(e.size_label is not None for e in result.entries):
        from ..policy import bucket_size

        root_size = bucket_size(total)
    else:
        root_size = human_size(total) if show_size else ""
    lines.append((result.root_label, root_size))

    by_path = {e.path: e for e in result.entries}
    children: dict[str, list[str]] = defaultdict(list)
    for e in result.entries:
        parent = e.path.rsplit("/", 1)[0] if "/" in e.path else ""
        children[parent].append(e.path)

    def rec(path: str, prefix: str) -> None:
        kids = children.get(path, [])
        for idx, cp in enumerate(kids):
            e = by_path[cp]
            last = idx == len(kids) - 1
            label = cp.rsplit("/", 1)[-1]
            if e.hidden:
                label += " (content hidden)"
            text = prefix + ("└── " if last else "├── ") + label
            size = (e.size_label or human_size(e.size)) if show_size else ""
            lines.append((text, size))
            rec(cp, prefix + ("    " if last else "│   "))

    rec("", "")
    return lines


def _truncate_cells(text: str, max_cells: int) -> str:
    """Truncate a string to a display-cell budget (CJK-aware), adding …."""
    if cell_len(text) <= max_cells:
        return text
    if max_cells <= 2:
        return "…"
    out, used = [], 0
    for ch in text:
        w = cell_len(ch)
        if used + w > max_cells - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def render_tree(result: ScanResult, show_size: bool = True,
                width: int | None = None) -> str:
    """Render the tree. When width is given (terminal size), the size column
    is pinned to the right edge and over-long names are truncated with … --
    without it, content wider than the terminal wraps and alignment breaks."""
    lines = to_tree_lines(result, show_size)
    if not show_size:
        return "\n".join(t for t, _ in lines)
    swidth = max(len(s) for _, s in lines) if lines else 0
    if width is not None:
        name_budget = max(10, width - swidth - 2)
        lines = [(_truncate_cells(t, name_budget), s) for t, s in lines]
    twidth = max(cell_len(t) for t, _ in lines)
    out = []
    for text, size in lines:
        pad = " " * (twidth - cell_len(text))
        out.append(f"{text}{pad}  {size.rjust(swidth)}".rstrip())
    return "\n".join(out)


def stats_line(result: ScanResult) -> str:
    return (
        f"{result.files} files, {result.dirs} dirs, "
        f"{result.hidden_dirs} hidden subtree(s)"
    )


def mappings_to_json(pseudo) -> str:
    return json.dumps(
        [
            {"category": c, "surface": s, "alias": a}
            for c, s, a in pseudo.all_mappings()
        ],
        ensure_ascii=False,
        indent=2,
    )
