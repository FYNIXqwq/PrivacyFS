"""Metadata-only, bounded-memory preview. Never authorizes model input."""
from collections import Counter, OrderedDict
import fnmatch
from pathlib import Path
import time

from ..scanner import PRIVATE_STATE_MARKER, has_private_state_marker
from .native import DIRECTORY, REPARSE, NativeVolume, NtfsUnavailable, NtfsCancelled, valid_name


class MemoryBudget(Exception):
    pass


class Preview:
    preliminary = True

    def __init__(self, root_id, exclusions):
        self.root_id = self.selected_id = root_id
        self.directories = {root_id:(root_id,DIRECTORY,".")}
        self.files, self.children, self.markers = [], {}, set()
        self.patterns = tuple(p.casefold() for p in exclusions)
        self.cache = OrderedDict()
        self.stats = Counter(ntfs_preview_only=1)

    def close(self):
        self.directories.clear()
        self.files.clear()
        self.children.clear()
        self.markers.clear()
        self.cache.clear()

    def path(self, identifier):
        original, parts, seen = identifier, [], set()
        base = ""
        while identifier != self.selected_id:
            if identifier in self.cache:
                base = self.cache[identifier]
                break
            if identifier in seen or len(seen) >= 4096 or identifier not in self.directories:
                raise NtfsUnavailable("invalid_parent_graph")
            seen.add(identifier)
            parent, attrs, name = self.directories[identifier]
            parts.append(name)
            identifier = parent
        result = "/".join(([base] if base else []) + list(reversed(parts)))
        if len(result) > 32760:
            raise NtfsUnavailable("invalid_parent_graph")
        self.cache[original] = result
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return result

    def select(self, scope):
        for identifier, (parent, attrs, name) in self.directories.items():
            if identifier != parent:
                self.children.setdefault(parent, []).append(identifier)
        identifier = self.root_id
        for component in Path(scope).parts:
            if identifier in self.markers or self.directories[identifier][1] & REPARSE:
                raise NtfsUnavailable("protected_scope")
            matches = [i for i in self.children.get(identifier, ())
                       if self.directories[i][2].casefold() == component.casefold()]
            exact = [i for i in matches if self.directories[i][2] == component]
            matches = exact or matches
            if len(matches) != 1:
                raise NtfsUnavailable("scope_changed")
            identifier = matches[0]
        self.selected_id = identifier

    def excluded(self, name):
        return any(fnmatch.fnmatchcase(name.casefold(), p) for p in self.patterns)

    def rows(self):
        pending, allowed = [self.selected_id], set()
        while pending:
            identifier = pending.pop()
            if identifier in allowed:
                raise NtfsUnavailable("invalid_parent_graph")
            parent, attrs, name = self.directories[identifier]
            if attrs & REPARSE or identifier in self.markers:
                continue
            if identifier != self.selected_id and ((identifier & 0xffffffffffff) < 16 or self.excluded(name)):
                continue
            allowed.add(identifier)
            pending.extend(self.children.get(identifier, ()))
            if identifier != self.selected_id:
                relative = self.path(identifier)
                yield {"relative_path":relative, "name":name, "is_dir":True}
        for identifier, parent, attrs, name in self.files:
            if parent not in allowed or attrs & REPARSE or (identifier & 0xffffffffffff) < 16 or self.excluded(name):
                continue
            prefix = self.path(parent)
            relative = prefix + "/" + name if prefix else name
            yield {"relative_path":relative, "name":name, "is_dir":False}

    def entries(self):
        for row in self.rows():
            yield Path(row["relative_path"]), row["is_dir"], row["relative_path"].count("/")


def build_preview(root, exclusions, cancel, notify, source_factory=NativeVolume, scope=Path(""),
                  memory_budget=512*1024*1024):
    """Spill by rebuilding with encrypted SQLite if the estimated budget is exceeded.

    Only the metadata name is shown: aliases, live access and journal changes
    must be verified by build_inventory before AI. No silent truncation.
    """
    if has_private_state_marker(root):
        raise NtfsUnavailable("protected_scope")
    if cancel.is_set():
        raise NtfsCancelled()
    preview = None
    try:
        with source_factory(root) as source:
            preview = Preview(source.root_id, exclusions)
            estimate, last_notice = 0, 0
            for record in source.records(cancel):
                valid_name(record.name)
                if record.name == "." and record.file_id != source.root_id:
                    raise NtfsUnavailable("invalid_metadata")
                if record.attributes & DIRECTORY:
                    preview.directories[record.file_id] = (record.parent_id, record.attributes, record.name)
                    estimate += 512 + len(record.name)*2
                else:
                    preview.files.append((record.file_id, record.parent_id, record.attributes, record.name))
                    estimate += 256 + len(record.name)*2
                if record.name.casefold() == PRIVATE_STATE_MARKER.casefold():
                    preview.markers.add(record.parent_id)
                preview.stats["ntfs_records"] += 1
                if estimate > memory_budget:
                    raise MemoryBudget()
                if preview.stats["ntfs_records"] % 4096 == 0:
                    if cancel.is_set():
                        raise NtfsCancelled()
                    now = time.monotonic()
                    if now - last_notice >= .15:
                        notify(phase="批量读取 NTFS 名称（内存预览）", ntfs_records=preview.stats["ntfs_records"])
                        last_notice = now
            if cancel.is_set():
                raise NtfsCancelled()
            preview.select(scope)
            notify(phase="名称预览已读取，正在显示目录结构", ntfs_records=preview.stats["ntfs_records"])
        return preview
    except MemoryBudget:
        if preview is not None:
            preview.close()
        if cancel.is_set():
            raise NtfsCancelled()
        from .index import build_inventory
        notify(phase="预览超过内存预算，切换加密磁盘索引")
        return build_inventory(root, exclusions, cancel, notify, source_factory=source_factory, scope=scope, preliminary=True)
    except BaseException:
        if preview is not None:
            preview.close()
        raise
