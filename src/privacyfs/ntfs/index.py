"""Encrypted, disk-backed NTFS inventory built before any model input is emitted."""
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from ..scanner import has_private_state_marker, PRIVATE_STATE_MARKER
from ..state_keys import protector
from .native import NativeVolume, Record, NtfsUnavailable, NtfsCancelled, DIRECTORY, REPARSE, STRUCTURAL_REASONS, valid_name


def _id(value):
    return int(value).to_bytes(8, "little", signed=False)


class Inventory:
    def __init__(self, root):
        self.root, self.root_id = Path(root), None
        self.directory = Path(tempfile.mkdtemp(prefix="privacyfs-ntfs-"))
        self.database_path = self.directory / "names.db"
        self.db = None
        self._closed = False
        try:
            (self.directory / PRIVATE_STATE_MARKER).write_text('{"created_by":"privacyfs-ntfs"}', encoding="ascii")
            self.db = sqlite3.connect(str(self.database_path))
            self.db.execute("PRAGMA journal_mode=OFF")
            self.db.execute("PRAGMA synchronous=OFF")
            self.db.execute("PRAGMA cache_size=-4096")
            self.db.executescript("""
                CREATE TABLE payloads (id INTEGER PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE nodes (id BLOB PRIMARY KEY, parent BLOB, attrs INTEGER, batch INTEGER, position INTEGER, ordinal INTEGER);
                CREATE INDEX node_parent ON nodes(parent,ordinal);
                CREATE INDEX directory_parent ON nodes(parent) WHERE (attrs & 16) != 0;
                CREATE INDEX node_order ON nodes(ordinal);
                CREATE TABLE allowed (id BLOB PRIMARY KEY);
                CREATE TABLE queue (position INTEGER PRIMARY KEY, id BLOB);
                CREATE TABLE changed (id BLOB PRIMARY KEY);
                CREATE TABLE markers (id BLOB PRIMARY KEY, parent BLOB);
                CREATE INDEX marker_parent ON markers(parent);
                CREATE TABLE names (parent BLOB, digest BLOB, id BLOB, batch INTEGER, position INTEGER, ordinal INTEGER,
                                    PRIMARY KEY(parent,digest));
                CREATE INDEX name_order ON names(parent,ordinal);
            """)
            self.db.commit()
            self.provider = protector()
            self.key = os.urandom(32)
            self.cache, self.path_cache = OrderedDict(), OrderedDict()
            self.cache_records = 0
            self.internal_cache = OrderedDict()
            self.stats = Counter({k:0 for k in ("errors","excluded","protected","reparse","ntfs_records",
                "ntfs_journal_updates","ntfs_link_names","ntfs_late_changes","ntfs_internal")})
            self.sequence = 0
            self.name_sequence = 0
            self.marker_ids = set()
            self.name_buffers = OrderedDict()
            self.buffer_bytes = 0
        except Exception:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        if self.db is not None:
            self.db.close()
            self.db = None
        # Remove only files this instance created; never recursively delete a tree.
        for filename in ("names.db", PRIVATE_STATE_MARKER):
            path = self.directory / filename
            if path.exists():
                path.unlink()
        self.directory.rmdir()
        for name in ("cache", "path_cache", "internal_cache", "name_buffers"):
            if hasattr(self,name):
                getattr(self,name).clear()
        self._closed = True

    def _payload(self, records):
        self.sequence += 1
        values = [[r.file_id,r.parent_id,r.attributes,r.usn,r.name,r.reason] for r in records]
        data = self.provider.protect(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        self.db.execute("INSERT INTO payloads VALUES (?,?)", (self.sequence,data))
        return self.sequence

    def _record(self, batch, position):
        if batch not in self.cache:
            row = self.db.execute("SELECT data FROM payloads WHERE id=?",(batch,)).fetchone()
            self.cache[batch] = json.loads(self.provider.unprotect(row[0]))
            self.cache_records += len(self.cache[batch])
            # Compact directory batches must not reduce the effective cache
            # from 131k records to only 4k when files and directories are split.
            limit = 131072 if getattr(self, "preliminary", False) else 4096
            while len(self.cache) > 1 and (self.cache_records > limit or len(self.cache) > 4096):
                _, removed = self.cache.popitem(last=False)
                self.cache_records -= len(removed)
        self.cache.move_to_end(batch)
        return Record(*self.cache[batch][position])

    def put(self, records):
        records = list(records)
        if not records:
            return
        for r in records:
            valid_name(r.name)
            if r.name == "." and r.file_id != self.root_id:
                raise NtfsUnavailable("invalid_metadata")
        if getattr(self, "preliminary", False):
            # Hierarchy traversal must not decrypt thousands of file names to
            # retrieve a single directory. Keep directory payloads compact.
            latest = list({r.file_id:r for r in records}.values())
            directories = [r for r in latest if r.attributes & DIRECTORY]
            groups = [directories[i:i+128] for i in range(0,len(directories),128)]
            files = [r for r in latest if not r.attributes & DIRECTORY]
            if files:
                groups.append(files)
        else:
            groups = [records]
        for group in groups:
            batch = self._payload(group)
            self.db.executemany("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?)", [
                (_id(r.file_id),_id(r.parent_id),r.attributes,batch,i,r.file_id & 0x0000ffffffffffff) for i,r in enumerate(group)])
        for r in records:
            if r.file_id in self.marker_ids:
                self.db.execute("DELETE FROM markers WHERE id=?",(_id(r.file_id),))
                self.marker_ids.discard(r.file_id)
            if r.name.casefold() == PRIVATE_STATE_MARKER.casefold():
                self.db.execute("INSERT INTO markers VALUES (?,?)",(_id(r.file_id),_id(r.parent_id)))
                self.marker_ids.add(r.file_id)
        self.path_cache.clear()
        self.internal_cache.clear()

    def get(self, identifier):
        row = self.db.execute("SELECT batch,position FROM nodes WHERE id=?",(_id(identifier),)).fetchone()
        if row is None:
            raise NtfsUnavailable("incomplete_parent_graph")
        return self._record(*row)

    def internal(self, identifier):
        original = identifier
        if identifier in self.internal_cache:
            self.internal_cache.move_to_end(identifier)
            return self.internal_cache[identifier]
        seen = set()
        result = False
        while identifier != self.root_id:
            if identifier in seen or len(seen) > 4096:
                raise NtfsUnavailable("invalid_parent_graph")
            if (identifier & 0x0000ffffffffffff) < 16:
                result = True
                break
            if identifier in self.internal_cache:
                result = self.internal_cache[identifier]
                break
            seen.add(identifier)
            row = self.db.execute("SELECT parent,attrs FROM nodes WHERE id=?",(_id(identifier),)).fetchone()
            if row is None:
                raise NtfsUnavailable("incomplete_parent_graph")
            if not row[1] & DIRECTORY:
                raise NtfsUnavailable("invalid_parent_graph")
            identifier = int.from_bytes(row[0],"little")
        self.internal_cache[original] = result
        if len(self.internal_cache) > 8192:
            self.internal_cache.popitem(last=False)
        return result

    def path(self, identifier):
        if identifier == self.root_id:
            return Path("")
        if identifier in self.path_cache:
            self.path_cache.move_to_end(identifier)
            return self.path_cache[identifier]
        original, parts, seen, base = identifier, [], set(), Path("")
        while identifier != self.root_id:
            if identifier in seen or len(seen) > 4096:
                raise NtfsUnavailable("invalid_parent_graph")
            seen.add(identifier)
            if identifier in self.path_cache:
                base = self.path_cache[identifier]
                break
            node = self.get(identifier)
            if not node.attributes & DIRECTORY:
                raise NtfsUnavailable("invalid_parent_graph")
            parts.append(node.name)
            identifier = node.parent_id
        result = base.joinpath(*reversed(parts))
        if result.is_absolute() or len(str(result)) > 32760:
            raise NtfsUnavailable("invalid_parent_graph")
        self.path_cache[original] = result
        if len(self.path_cache) > 4096:
            self.path_cache.popitem(last=False)
        return result

    def add_names(self, records):
        # Pack aliases from the same directory together. Otherwise grouping the
        # final stream by parent would decrypt one scattered batch per filename.
        for record in records:
            valid_name(record.name)
            if record.name == ".":
                raise NtfsUnavailable("invalid_metadata")
            cost = 512 + len(record.name.encode("utf-8","surrogatepass"))
            self.name_sequence += 1
            values = self.name_buffers.setdefault(record.parent_id,[])
            values.append((record,self.name_sequence,cost))
            self.name_buffers.move_to_end(record.parent_id)
            self.buffer_bytes += cost
            if len(values) >= 64:
                self._flush_parent(record.parent_id)
            while self.buffer_bytes > 16*1024*1024:
                self._flush_parent(next(iter(self.name_buffers)))

    def _flush_parent(self, parent):
        values = self.name_buffers.pop(parent)
        self.buffer_bytes -= sum(value[2] for value in values)
        batch = self._payload([value[0] for value in values])
        for position,(record,ordinal,_) in enumerate(values):
            self._add_name(record,batch,position,ordinal)

    def flush_names(self):
        while self.name_buffers:
            self._flush_parent(next(iter(self.name_buffers)))

    def _add_name(self, record, batch, position, ordinal=None):
        valid_name(record.name)
        if record.name == ".":
            raise NtfsUnavailable("invalid_metadata")
        digest = hashlib.blake2b(record.name.encode("utf-16-le","surrogatepass"), key=self.key).digest()
        parent = _id(record.parent_id)
        existing = self.db.execute("SELECT id,batch,position FROM names WHERE parent=? AND digest=?",(parent,digest)).fetchone()
        if existing:
            if existing[0] != _id(record.file_id) or self._record(existing[1],existing[2]).name != record.name:
                raise NtfsUnavailable("conflicting_names")
            return
        if ordinal is None:
            self.name_sequence += 1
            ordinal = self.name_sequence
        self.db.execute("INSERT INTO names VALUES (?,?,?,?,?,?)", (parent,digest,_id(record.file_id),batch,position,ordinal))

    def add_directory(self, record):
        batch, position = self.db.execute("SELECT batch,position FROM nodes WHERE id=?",(_id(record.file_id),)).fetchone()
        self._add_name(record,batch,position)

    def children(self, identifier):
        cursor = self.db.execute("SELECT batch,position FROM nodes WHERE parent=? AND (attrs & 16) != 0 AND id != ?",
                                 (_id(identifier),_id(identifier)))
        for row in cursor:
            yield self._record(*row)

    def queue_children(self, identifier):
        self.db.execute("""INSERT INTO queue(id)
            SELECT id FROM nodes WHERE parent=? AND (attrs & 16) != 0 AND id != ?""",
            (_id(identifier),_id(identifier)))

    def files(self):
        for row in self.db.execute("SELECT batch,position FROM nodes WHERE (attrs & ?) = 0 ORDER BY ordinal", (DIRECTORY,)):
            yield self._record(*row)

    def entries(self):
        if getattr(self, "preliminary", False):
            query = """SELECT n.batch,n.position FROM nodes n JOIN allowed a ON n.parent=a.id
                WHERE n.id != n.parent AND ((n.attrs & ?) = 0 OR EXISTS(SELECT 1 FROM allowed d WHERE d.id=n.id))
                ORDER BY n.batch,n.position"""
            for row in self.db.execute(query, (DIRECTORY,)):
                record = self._record(*row)
                if record.file_id == self.selected_id or record.attributes & REPARSE:
                    continue
                if any(fnmatch.fnmatchcase(record.name.casefold(), p) for p in self.patterns):
                    continue
                relative = (self.path(record.parent_id) / record.name).relative_to(self.scope)
                yield relative, bool(record.attributes & DIRECTORY), len(relative.parts)-1
            return
        for row in self.db.execute("SELECT batch,position FROM names ORDER BY parent,ordinal"):
            record = self._record(*row)
            relative = self.path(record.parent_id) / record.name
            relative = relative.relative_to(getattr(self, "scope", Path("")))
            yield relative, bool(record.attributes & DIRECTORY), len(relative.parts)-1


def build_inventory(root, exclusions, cancel, notify, source_factory=NativeVolume, scope=Path(""), preliminary=False, lazy=False,
                    existing=None):
    if has_private_state_marker(root):
        raise NtfsUnavailable("protected_scope")
    inventory = existing if existing is not None else Inventory(root)
    if existing is not None and not getattr(existing,"lazy",False):
        raise NtfsUnavailable("invalid_index_state")
    inventory.preliminary = preliminary
    inventory.lazy = False
    inventory.stats["ntfs_preview_only"] = int(preliminary)
    patterns = tuple(p.casefold() for p in exclusions)
    last_notice, last_phase = 0, None
    def progress(phase, **values):
        nonlocal last_notice, last_phase
        now = time.monotonic()
        if phase != last_phase or now-last_notice >= 0.15:
            notify(phase=phase, **values)
            last_notice, last_phase = now, phase
    def excluded(name):
        return any(fnmatch.fnmatchcase(name.casefold(),p) for p in patterns)
    def check():
        if cancel.is_set():
            raise NtfsCancelled()
    def sqlite_progress():
        if cancel.is_set():
            return 1
        progress(last_phase or "处理 NTFS 索引")
        return int(cancel.is_set())
    inventory.db.set_progress_handler(sqlite_progress, 20000)
    try:
        with source_factory(root) as source:
            identity = (source.root_id,getattr(source,"serial",None))
            if existing is not None:
                if inventory.volume_identity != identity or inventory.preview_start is None:
                    raise NtfsUnavailable("volume_changed")
                start = inventory.preview_start
                progress("复用已有索引，合并预览期间的名称变动", ntfs_records=inventory.stats["ntfs_records"])
            else:
                inventory.root_id = source.root_id
                inventory.volume_identity = identity
                inventory.put([Record(source.root_id,source.root_id,DIRECTORY,0,".")])
                start = source.journal() if not preliminary else None
                inventory.preview_start = None
                if preliminary and lazy:
                    try:
                        inventory.preview_start = source.journal()
                    except NtfsUnavailable:
                        pass  # Browsing remains available; verification must re-read.
                records = iter(source.records(cancel))
                while chunk := list(itertools.islice(records,4096 if preliminary else 128)):
                    check()
                    inventory.put(chunk)
                    inventory.stats["ntfs_records"] += len(chunk)
                    if inventory.stats["ntfs_records"] % 4096 == 0:
                        inventory.db.commit()
                    progress("读取 NTFS 名称索引", ntfs_records=inventory.stats["ntfs_records"])
            end = source.journal() if not preliminary else None
            for record in (() if preliminary else source.changes(start,end,cancel)):
                check()
                if record.reason & STRUCTURAL_REASONS:
                    inventory.db.execute("INSERT OR IGNORE INTO changed VALUES (?)",(_id(record.file_id),))
            updates = []
            for (identifier,) in inventory.db.execute("SELECT id FROM changed"):
                check()
                identifier = int.from_bytes(identifier,"little")
                current = source.current_record(identifier)
                if current is None:
                    inventory.db.execute("DELETE FROM nodes WHERE id=?",(_id(identifier),))
                    inventory.db.execute("DELETE FROM markers WHERE id=?",(_id(identifier),))
                else:
                    updates.append(current)
                    if len(updates) == 128:
                        inventory.put(updates)
                        updates = []
                inventory.stats["ntfs_journal_updates"] += 1
            inventory.put(updates)
            inventory.db.commit()
            # NTFS enumerates a volume. Only the explicitly selected subtree is
            # admitted to the directory queue and the eventual model inventory.
            selected_id = source.root_id
            selected_path = Path("")
            scope_parts = Path(scope).parts
            if lazy and scope_parts and hasattr(source,"directory_identity"):
                selected_id = source.directory_identity(root / scope)
                selected_path = inventory.path(selected_id)
                if selected_path != Path(scope):
                    raise NtfsUnavailable("scope_changed")
                ancestor = selected_id
                while True:
                    check()
                    record = inventory.get(ancestor)
                    if record.attributes & REPARSE or inventory.db.execute(
                            "SELECT 1 FROM markers WHERE parent=?",(_id(ancestor),)).fetchone():
                        raise NtfsUnavailable("protected_scope")
                    if ancestor == source.root_id:
                        break
                    ancestor = record.parent_id
                scope_parts = ()
            for component in scope_parts:
                check()
                parent = inventory.get(selected_id)
                if (parent.attributes & REPARSE or has_private_state_marker(root / selected_path)
                        or inventory.db.execute("SELECT 1 FROM markers WHERE parent=?", (_id(selected_id),)).fetchone()):
                    raise NtfsUnavailable("protected_scope")
                if not preliminary and source.gate_directory(root / selected_path, selected_id) != "ok":
                    raise NtfsUnavailable("access_denied")
                matches = [r for r in inventory.children(selected_id) if r.name.casefold() == component.casefold()]
                exact = [r for r in matches if r.name == component]
                matches = exact or matches
                if len(matches) != 1:
                    raise NtfsUnavailable("scope_changed")
                selected_id = matches[0].file_id
                selected_path /= matches[0].name
            inventory.scope = selected_path
            inventory.selected_id, inventory.patterns = selected_id, patterns
            if preliminary and lazy:
                inventory.db.commit()
                inventory.lazy = True
                inventory.stats["ntfs_preview_only"] = 1
                progress("NTFS 索引已就绪，按需读取目录页面", ntfs_records=inventory.stats["ntfs_records"])
                return inventory
            progress("构建预览层级（尚未核验）" if preliminary else "校验目录访问范围",
                     ntfs_records=inventory.stats["ntfs_records"], ntfs_preliminary=int(preliminary),
                     ntfs_preview_directories=0)
            inventory.db.execute("INSERT INTO queue(id) VALUES (?)",(_id(selected_id),))
            directory_count = 0
            while pending := inventory.db.execute("SELECT position,id FROM queue ORDER BY position DESC LIMIT 1").fetchone():
                check()
                inventory.db.execute("DELETE FROM queue WHERE position=?",(pending[0],))
                identifier = int.from_bytes(pending[1],"little")
                record = inventory.get(identifier)
                if identifier != selected_id:
                    if inventory.internal(identifier):
                        inventory.stats["ntfs_internal"] += 1
                        continue
                    if excluded(record.name):
                        inventory.stats["excluded"] += 1
                        continue
                if record.attributes & REPARSE:
                    inventory.stats["reparse"] += 1
                    continue
                relative = Path("") if preliminary else inventory.path(identifier)
                if (inventory.db.execute("SELECT 1 FROM markers WHERE parent=?",(_id(identifier),)).fetchone()
                        or (not preliminary and has_private_state_marker(root / relative))):
                    inventory.stats["protected"] += 1
                    continue
                access = "ok" if preliminary else source.gate_directory(root/relative,identifier)
                if access == "reparse":
                    inventory.stats["reparse"] += 1
                    continue
                if identifier != selected_id and not preliminary:
                    inventory.add_directory(record)
                if access != "ok":
                    inventory.stats["errors"] += 1
                    continue
                inventory.db.execute("INSERT INTO allowed VALUES (?)",(_id(identifier),))
                inventory.queue_children(identifier)
                directory_count += 1
                if directory_count % 256 == 0:
                    check()
                if preliminary:
                    progress("构建预览层级（尚未核验）", ntfs_preview_directories=directory_count)
                else:
                    progress("校验 NTFS 目录", ntfs_directories=directory_count)
            inventory.db.commit()
            if preliminary:
                check()
                inventory.stats["ntfs_preview_only"] = 1
                inventory.stats["ntfs_preview_directories"] = directory_count
                progress("名称索引预览就绪（硬链接及访问范围尚未核验）", ntfs_records=inventory.stats["ntfs_records"],
                         ntfs_preview_directories=directory_count)
                return inventory
            progress("补齐硬链接名称", ntfs_directories=directory_count)
            files = (r for r in inventory.files() if not inventory.internal(r.parent_id)
                     and (r.file_id & 0x0000ffffffffffff) >= 16)
            # Bounded submission, unlike executor.map over the entire volume.
            with ThreadPoolExecutor(max_workers=8) as pool:
                while chunk := list(itertools.islice(files,64)):
                    check()
                    names = []
                    for record, result in zip(chunk, pool.map(source.file_links,[r.file_id for r in chunk])):
                        check()
                        if result is None:
                            inventory.stats["errors"] += 1
                            continue
                        attrs, aliases = result
                        if attrs & REPARSE:
                            inventory.stats["reparse"] += 1
                            continue
                        if attrs & DIRECTORY:
                            raise NtfsUnavailable("stale_identity")
                        for parent, name in aliases:
                            if name.casefold() == PRIVATE_STATE_MARKER.casefold() and not inventory.db.execute(
                                    "SELECT 1 FROM markers WHERE parent=?",(_id(parent),)).fetchone():
                                raise NtfsUnavailable("privacy_boundary_changed")
                            allowed = inventory.db.execute("SELECT 1 FROM allowed WHERE id=?",(_id(parent),)).fetchone()
                            if not allowed:
                                inventory.internal(parent)  # Validate even excluded ancestry; no missing/cyclic parent is safe.
                                continue
                            if excluded(name):
                                inventory.stats["excluded"] += 1
                                continue
                            names.append(Record(record.file_id,parent,attrs,0,name))
                            if len(names) >= 128:
                                inventory.add_names(names)
                                names = []
                            inventory.stats["ntfs_link_names"] += 1
                    inventory.add_names(names)
                    inventory.db.commit()
                    progress("补齐硬链接名称", ntfs_link_names=inventory.stats["ntfs_link_names"])
            check()
            inventory.flush_names()
            inventory.db.commit()
            final = source.journal()
            for record in source.changes(end,final,cancel):
                if record.reason & STRUCTURAL_REASONS and (
                        record.name.casefold() == PRIVATE_STATE_MARKER.casefold()
                        or inventory.db.execute("SELECT 1 FROM allowed WHERE id=?",(_id(record.parent_id),)).fetchone()
                        or not inventory.db.execute("SELECT 1 FROM nodes WHERE id=?",(_id(record.parent_id),)).fetchone()):
                    inventory.stats["ntfs_late_changes"] += 1
            if inventory.stats["ntfs_late_changes"]:
                inventory.stats["errors"] += 1
            check()
            progress("NTFS 索引就绪", ntfs_records=inventory.stats["ntfs_records"],
                     ntfs_link_names=inventory.stats["ntfs_link_names"], ntfs_directories=directory_count)
        return inventory
    except BaseException:
        inventory.close()
        if cancel.is_set():
            raise NtfsCancelled() from None
        raise
    finally:
        if inventory.db is not None:
            inventory.db.set_progress_handler(None, 0)


def try_inventory(root, recursive, exclusions, cancel, notify, *, allow_subdirectory=False, preliminary=False, existing=None):
    if cancel.is_set():
        raise NtfsCancelled()
    if os.name != "nt":
        raise NtfsUnavailable("not_windows")
    if not recursive:
        raise NtfsUnavailable("scope_not_volume")
    volume_root, scope = root, Path("")
    if allow_subdirectory:
        if not re.fullmatch(r"[A-Za-z]:\\", root.anchor):
            raise NtfsUnavailable("scope_not_volume")
        volume_root = Path(root.anchor)
        scope = root.relative_to(volume_root)
        # Do not reinterpret a junction/mounted volume as its lexical drive.
        if root.resolve() != root.absolute():
            raise NtfsUnavailable("scope_redirected")
    elif not re.fullmatch(r"[A-Za-z]:\\",str(root)):
        raise NtfsUnavailable("scope_not_volume")
    try:
        if preliminary:
            return build_inventory(volume_root,exclusions,cancel,notify,scope=scope,preliminary=True,lazy=True)
        if existing is not None:
            if existing.root != volume_root:
                raise NtfsUnavailable("scope_changed")
            if getattr(existing,"preview_start",None) is None:
                existing.close()
                existing = None
                notify(phase="缺少预览日志检查点，重新读取核验索引")
        return build_inventory(volume_root,exclusions,cancel,notify,scope=scope,preliminary=preliminary,existing=existing)
    except (NtfsUnavailable, NtfsCancelled):
        raise
    except Exception:
        raise NtfsUnavailable("index_failed") from None
