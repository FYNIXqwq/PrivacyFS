"""D4 scoped, authenticated state with encrypted Windows cache payloads."""
from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path
import re
import uuid
import zlib

from .release import (ReleaseStore, STORE_MARKER, canonical, checked_directory,
                      regular, write_new, control_bytes, disjoint)
from .state_keys import protector, wrapped_key
from .task_profiles import strict_json, load_task_profile
from .relations import load_relation_config
from .verifier import ReleaseRejected

STATE_VERSION = "d4-workspace-1"
MAX_RECORD = 64 * 1024 * 1024


class WorkspaceStore:
    def __init__(self, releases: ReleaseStore):
        self.releases = releases
        self.root = releases.root / "workspaces"
        if not self.root.exists():
            self.root.mkdir(mode=0o700)
        checked_directory(self.root)
        self.protector = protector()

    def token(self, *values):
        return self.releases.mac(canonical(values))

    def directory(self, workspace_id):
        if not isinstance(workspace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", workspace_id):
            raise ValueError("invalid workspace identifier")
        return self.root / workspace_id

    def empty_shell(self, workspace_id):
        directory = checked_directory(self.directory(workspace_id))
        names = {p.name for p in directory.iterdir()}
        if not names:
            return True
        if names == {"cache"}:
            return not any(checked_directory(directory / "cache").iterdir())
        return False

    def _write(self, path, value, *, directory_limit=None):
        raw = canonical(value)
        if len(raw) > MAX_RECORD:
            raise ReleaseRejected("WORKSPACE_RECORD_LIMIT")
        compressed = zlib.compress(raw)
        encrypted = self.protector.protect(compressed)
        envelope = canonical({"provider": self.protector.name, "blob": base64.b64encode(encrypted).decode(),
                              "mac": self.releases.mac(self.protector.name.encode("ascii") + b"\0" + encrypted)})
        if directory_limit is not None and sum(regular(p).st_size for p in path.parent.iterdir() if p != path) + len(envelope) > directory_limit:
            raise ReleaseRejected("WORKSPACE_CACHE_LIMIT")
        if path.exists():
            regular(path)
        temporary = path.with_name(uuid.uuid4().hex + ".tmp")
        try:
            write_new(temporary, envelope)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                regular(temporary)
                temporary.unlink()

    def _read(self, path):
        if regular(path).st_size > MAX_RECORD * 2:
            raise ReleaseRejected("WORKSPACE_RECORD_LIMIT")
        envelope = strict_json(path.read_text(encoding="utf-8"))
        try:
            if set(envelope) != {"provider", "blob", "mac"}:
                raise ValueError()
            if not isinstance(envelope["provider"], str):
                raise ValueError()
            encrypted = base64.b64decode(envelope["blob"], validate=True)
            if not hmac.compare_digest(self.releases.mac(envelope["provider"].encode("ascii") + b"\0" + encrypted), envelope["mac"]):
                raise ValueError()
            compressed = protector(envelope["provider"]).unprotect(encrypted)
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, MAX_RECORD + 1)
            if len(raw) > MAX_RECORD or not decoder.eof or decoder.unused_data:
                raise ValueError()
            return strict_json(raw.decode("utf-8"))
        except (ValueError, KeyError, TypeError, zlib.error):
            raise ReleaseRejected("WORKSPACE_INTEGRITY_FAILED") from None

    def load(self, workspace_id):
        directory = checked_directory(self.directory(workspace_id))
        record = self._read(directory / "index.json")
        if record.get("schema_version") != STATE_VERSION or record.get("workspace_id") != workspace_id:
            raise ReleaseRejected("WORKSPACE_SCHEMA_MISMATCH")
        return record

    def save(self, record):
        directory = checked_directory(self.directory(record["workspace_id"]))
        self._write(directory / "index.json", record)

    def cache_path(self, workspace_id, key):
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid cache identifier")
        return self.directory(workspace_id) / "cache" / (key + ".json")

    def cache_read(self, workspace_id, key):
        path = self.cache_path(workspace_id, key)
        if not path.exists():
            return None
        checked_directory(path.parent)
        value = self._read(path)
        if value.get("cache_key") != key or value.get("workspace_id") != workspace_id:
            raise ReleaseRejected("CACHE_SCOPE_MISMATCH")
        return value["data"]

    def cache_write(self, workspace_id, key, data, *, replace_existing=False):
        path = self.cache_path(workspace_id, key)
        checked_directory(path.parent)
        if not path.exists() or replace_existing:
            used = sum(regular(p).st_size for p in path.parent.glob("*.json"))
            if used > 128 * 1024 * 1024 or len(list(path.parent.iterdir())) >= 3000:
                raise ReleaseRejected("WORKSPACE_CACHE_LIMIT")
            self._write(path, {"cache_key": key, "workspace_id": workspace_id, "data": data}, directory_limit=128 * 1024 * 1024)

    def invalidate_plans(self, record):
        invalidated = 0
        for path in (self.releases.root / "plans").glob("*.json"):
            plan = self.releases.load(path.stem)
            if plan["request"].get("workspace_id") == record["workspace_id"] and plan["state"] not in {"PUBLISHED", "STAGING", "RECONCILING"}:
                if plan["binding"].get("workspace_revision") != record["revision"] or record["status"] != "ready":
                    plan["state"], plan["approval"] = "NEEDS_REVIEW", None
                    self.releases.save(plan)
                    invalidated += 1
        return invalidated

    def prune(self, record, *, dry_run=True):
        directory = checked_directory(self.directory(record["workspace_id"]) / "cache")
        referenced = {f["cache"] for f in record["files"].values()}
        referenced.update(record["components"].values())
        candidates = []
        for path in directory.iterdir():
            if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                if re.fullmatch(r"[0-9a-f]{32}\.tmp", path.name):
                    candidates.append(path)
                    continue
                raise ReleaseRejected("UNKNOWN_WORKSPACE_CONTENT")
            regular(path)
            if path.stem not in referenced:
                candidates.append(path)
        total = sum(regular(p).st_size for p in candidates)
        if not dry_run:
            for path in candidates:
                path.unlink()
        return {"cache_files": len(candidates), "bytes": total, "dry_run": dry_run}


def protect_existing_key(releases):
    path = releases.root / STORE_MARKER
    data = strict_json(control_bytes(path).decode("utf-8"))
    if data.get("schema_version") != "d4-key-1":
        updated = wrapped_key(releases.key)
        temporary = releases.root / (uuid.uuid4().hex + ".key.tmp")
        try:
            write_new(temporary, canonical(updated))
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                regular(temporary)
                temporary.unlink()
    # Per-record atomic migration, resumable after any interruption. The same
    # key and approval bindings are retained, including publication history.
    for plan in (releases.root / "plans").glob("*.json"):
        record = releases.load(plan.stem)
        if "private" not in strict_json(plan.read_text(encoding="utf-8")):
            releases.save(record)


def create_workspace(root, relations, profile, state_dir):
    source = checked_directory(root)
    state = Path(os.path.abspath(state_dir))
    if not disjoint(source, state):
        raise ReleaseRejected("STATE_SOURCE_OVERLAP")
    request = {"root": str(source), "relations": str(Path(relations).absolute()), "profile": str(Path(profile).absolute())}
    load_relation_config(Path(request["relations"]))
    load_task_profile(Path(request["profile"]))
    releases = ReleaseStore(state, create=True)
    with releases.lock():
        protect_existing_key(releases)
        store = WorkspaceStore(releases)
        for directory in store.root.iterdir():
            if not (directory / "index.json").exists() and store.empty_shell(directory.name):
                continue
            existing = store.load(directory.name)
            if existing["request"] == request:
                return {"workspace_id": existing["workspace_id"], "revision": existing["revision"], "status": existing["status"]}
        if len(list(store.root.iterdir())) >= 100:
            raise ReleaseRejected("WORKSPACE_COUNT_LIMIT")
        workspace_id = uuid.uuid4().hex
        directory = store.directory(workspace_id)
        directory.mkdir(mode=0o700)
        (directory / "cache").mkdir(mode=0o700)
        record = {"schema_version": STATE_VERSION, "workspace_id": workspace_id, "revision": 0,
                  "request": request, "status": "new", "files": {}, "components": {},
                  "controls": {}, "corrections": [], "events": [], "keep_cache": False}
        store.save(record)
        return {"workspace_id": workspace_id, "revision": 0, "status": "new"}
