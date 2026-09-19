"""Read bounded pages from the original encrypted name index, without a tree walk."""
import time
from pathlib import PurePosixPath
from .native import DIRECTORY, REPARSE, NtfsUnavailable, NtfsCancelled
from .index import _id


def _blocked(index, record):
    import fnmatch
    return (record.attributes & REPARSE or
            index.db.execute("SELECT 1 FROM markers WHERE parent=?", (_id(record.file_id),)).fetchone() or
            record.file_id != index.selected_id and (
                (record.file_id & 0xffffffffffff) < 16 or
                any(fnmatch.fnmatchcase(record.name.casefold(),p) for p in index.patterns)))


def _directory(index, parent, identifier):
    if not isinstance(parent,str) or len(parent) > 32760:
        raise NtfsUnavailable("invalid_page_scope")
    parts = PurePosixPath(parent).parts if parent != "." else ()
    if (not isinstance(parent,str) or parent.startswith(("/","\\")) or
            any(p in {"..","."} or ":" in p or "\\" in p for p in parts)):
        raise NtfsUnavailable("invalid_page_scope")
    if identifier is None:
        identifier = index.selected_id
        for component in parts:
            candidates = [r for r in index.children(identifier) if r.name.casefold() == component.casefold()]
            exact = [r for r in candidates if r.name == component]
            candidates = exact or candidates
            if len(candidates) != 1:
                raise NtfsUnavailable("invalid_page_scope")
            identifier = candidates[0].file_id
    identifier = int(identifier)
    current, names, seen = identifier, [], set()
    while True:
        if current in seen or len(seen) >= 4096:
            raise NtfsUnavailable("invalid_parent_graph")
        seen.add(current)
        record = index.get(current)
        if not record.attributes & DIRECTORY or _blocked(index,record):
            raise NtfsUnavailable("page_scope_blocked")
        if current == index.selected_id:
            break
        names.append(record.name)
        if record.parent_id == current:
            raise NtfsUnavailable("invalid_page_scope")
        current = record.parent_id
    relative = "/".join(reversed(names)) or "."
    if relative != parent:
        raise NtfsUnavailable("invalid_page_scope")
    return identifier


def browse(index, parent=".", after=0, limit=81, directory_id=None, cancel=None):
    """Only batches needed by visible records and their ancestors are decrypted.

    A filtered page can require continuation; never skip the unvisited remainder.
    Private NTFS IDs remain in this UI-only protocol, never in saved reports/AI.
    """
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 81:
        raise NtfsUnavailable("invalid_page_request")
    identifier = _directory(index,parent,directory_id)
    import fnmatch
    rows, cursor, scanned = [], after, 0
    deadline = time.monotonic()+.04
    while True:
        if cancel is not None and cancel.is_set():
            raise NtfsCancelled()
        records = index.db.execute("""SELECT id,attrs,batch,position,ordinal FROM nodes
            WHERE parent=? AND ordinal>? AND id!=? ORDER BY ordinal LIMIT 128""",
            (_id(identifier),cursor-1,_id(identifier))).fetchall()
        if not records:
            return {"rows":rows,"cursor":cursor,"pending":False}
        for fid, attrs, batch, position, ordinal in records:
            if cancel is not None and cancel.is_set():
                raise NtfsCancelled()
            cursor = ordinal+1
            scanned += 1
            record = index._record(batch,position)
            if (record.file_id & 0xffffffffffff) >= 16 and not attrs & REPARSE and not any(
                    fnmatch.fnmatchcase(record.name.casefold(),p) for p in index.patterns):
                private = attrs & DIRECTORY and index.db.execute(
                    "SELECT 1 FROM markers WHERE parent=?",(fid,)).fetchone()
                if not private:
                    relative = record.name if parent == "." else parent+"/"+record.name
                    if len(relative) > 32760:
                        raise NtfsUnavailable("invalid_parent_graph")
                    rows.append({"entry_id":f"P{cursor}","relative_path":relative,
                        "name":record.name,"is_dir":bool(attrs & DIRECTORY), "verdict":None,
                        "_directory_id":str(record.file_id) if attrs & DIRECTORY else None})
            if len(rows) == limit:
                return {"rows":rows,"cursor":cursor,"pending":False}
            if scanned >= 2048 or time.monotonic() >= deadline:
                return {"rows":rows,"cursor":cursor,"pending":True}
