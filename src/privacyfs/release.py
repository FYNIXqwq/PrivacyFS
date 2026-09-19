"""Private local D3 plan store and fail-closed, append-only artifact publishing."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import uuid
import base64
import zlib

from .documents import DocumentSession, Limits
from .documents.models import ProcessingStopped
from .documents.session import _check_regular_path
from .emit.mirror import _destination_lock
from .evidence import EvidenceGraph, RULE_VERSION, ATTACK_VERSION
from .normalization import NORMALIZATION_VERSION
from .planning import build_plan, public_plan, PLANNER_VERSION
from .relations import load_relation_config
from .task_profiles import load_task_profile, read_config, strict_json, PROFILE_VERSION
from .verifier import verify_candidate, ReleaseRejected, VERIFIER_VERSION, ResidualCheck

STORE_MARKER = ".privacyfs-state.json"
RELEASE_MARKER = ".privacyfs-release.json"
STORE_VERSION = "d3-state-1"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def disjoint(a, b):
    return not (a.is_relative_to(b) or b.is_relative_to(a))


def checked_directory(path):
    path = Path(os.path.abspath(path))
    for item in reversed([path, *path.parents]):
        _check_regular_path(item, directory=True)
    return path.resolve()


def regular(path):
    info = _check_regular_path(path)
    if info.st_nlink != 1:
        raise ReleaseRejected("SHARED_STATE_OR_OUTPUT_FILE")
    return info


def no_alternate_streams(path):
    if os.name != "nt":
        if hasattr(os, "listxattr") and os.listxattr(path):
            raise ReleaseRejected("OUTPUT_EXTENDED_METADATA")
        return
    import ctypes
    from ctypes import wintypes
    class StreamData(ctypes.Structure):
        _fields_ = [("size", ctypes.c_longlong), ("name", wintypes.WCHAR * 296)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(StreamData), wintypes.DWORD]
    kernel.FindFirstStreamW.restype = wintypes.HANDLE
    kernel.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    kernel.FindNextStreamW.restype = wintypes.BOOL
    kernel.FindClose.argtypes = [wintypes.HANDLE]
    data = StreamData()
    handle = kernel.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
    if handle == ctypes.c_void_p(-1).value:
        if ctypes.get_last_error() in {38, 87}:
            return  # no streams, or filesystem does not support them
        raise ReleaseRejected("OUTPUT_METADATA_CHECK_FAILED")
    try:
        while True:
            if data.name != "::$DATA":
                raise ReleaseRejected("OUTPUT_ALTERNATE_STREAM")
            if not kernel.FindNextStreamW(handle, ctypes.byref(data)):
                if ctypes.get_last_error() != 38:
                    raise ReleaseRejected("OUTPUT_METADATA_CHECK_FAILED")
                break
    finally:
        kernel.FindClose(handle)


def private_directory(path, *, create=False):
    """Owner-only state root. This is not isolation from the same OS account."""
    if create:
        path.mkdir(mode=0o700)
    checked_directory(path)
    if os.name == "nt":
        # LiteralPath and an environment variable keep path text out of code.
        common = "$ErrorActionPreference='Stop'; $p=$env:PRIVACYFS_PRIVATE_PATH; $sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
        if create:
            script = common + "$acl=[System.IO.Directory]::GetAccessControl($p); $acl.SetAccessRuleProtection($true,$false); foreach($old in @($acl.Access)){$acl.RemoveAccessRuleSpecific($old)}; $r=New-Object System.Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'); $acl.AddAccessRule($r); [System.IO.Directory]::SetAccessControl($p,$acl)"
        else:
            script = common + "$acl=[System.IO.Directory]::GetAccessControl($p); if(-not $acl.AreAccessRulesProtected){exit 1}; if($acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){exit 1}; foreach($r in $acl.Access){if($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){exit 1}}"
        try:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env={**os.environ, "PRIVACYFS_PRIVATE_PATH": str(path)}, capture_output=True,
                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ReleaseRejected("PRIVATE_STATE_PERMISSION_CHECK_TIMEOUT") from None
        if result.returncode:
            raise ReleaseRejected("PRIVATE_STATE_PERMISSIONS_REQUIRED")
    else:
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ReleaseRejected("PRIVATE_STATE_PERMISSIONS_REQUIRED")


def write_new(path, payload):
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def control_bytes(path):
    path = Path(os.path.abspath(path))
    checked_directory(path.parent)
    before = regular(path)
    payload = read_config(path)
    after = regular(path)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ReleaseRejected("CONTROL_CHANGED")
    return payload


class ReleaseStore:
    def __init__(self, root, *, create=False):
        self.root = Path(os.path.abspath(root))
        checked_directory(self.root.parent)
        exists = os.path.lexists(self.root)
        if not exists and not create:
            raise ValueError("state directory does not exist")
        if not exists:
            private_directory(self.root, create=True)
            self.key = secrets.token_bytes(32)
            from .state_keys import wrapped_key
            write_new(self.root / STORE_MARKER, canonical(wrapped_key(self.key)))
            (self.root / "plans").mkdir(mode=0o700)
            (self.root / "staging").mkdir(mode=0o700)
        else:
            private_directory(self.root)
            marker = self.root / STORE_MARKER
            if regular(marker).st_size > 4096:
                raise ValueError("invalid state marker")
            data = strict_json(marker.read_text(encoding="utf-8"))
            if data.get("schema_version") == "d4-key-1":
                from .state_keys import unwrap_key
                self.key = unwrap_key(data)
            elif set(data) == {"schema_version", "key"} and data["schema_version"] == STORE_VERSION:
                self.key = bytes.fromhex(data["key"])
            else:
                raise ValueError("unsupported private state schema")
            if len(self.key) != 32:
                raise ValueError("invalid state key")
        checked_directory(self.root / "plans")
        checked_directory(self.root / "staging")

    def mac(self, value):
        return hmac.new(self.key, value, hashlib.sha256).hexdigest()

    @contextmanager
    def lock(self):
        lock = self.root / ".lock"
        if os.path.lexists(lock):
            regular(lock)
        with _destination_lock(lock):
            yield

    def _path(self, plan_id):
        if not isinstance(plan_id, str) or len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("invalid plan identifier")
        return self.root / "plans" / (plan_id + ".json")

    def load(self, plan_id):
        path = self._path(plan_id)
        if regular(path).st_size > 8 * 1024 * 1024:
            raise ValueError("state record limit")
        data = strict_json(path.read_text(encoding="utf-8"))
        if set(data) == {"payload", "mac"}:
            if not hmac.compare_digest(self.mac(canonical(data["payload"])), data["mac"]):
                raise ReleaseRejected("STATE_INTEGRITY_FAILED")
            record = data["payload"]
        elif set(data) == {"payload", "provider", "private", "mac"}:
            from .state_keys import protector
            unsigned = {k: v for k, v in data.items() if k != "mac"}
            if not hmac.compare_digest(self.mac(canonical(unsigned)), data["mac"]):
                raise ReleaseRejected("STATE_INTEGRITY_FAILED")
            try:
                encrypted = base64.b64decode(data["private"], validate=True)
                packed = protector(data["provider"]).unprotect(encrypted)
                decoder = zlib.decompressobj()
                raw = decoder.decompress(packed, 4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
                    raise ValueError()
                record = strict_json(raw.decode("utf-8"))
                if data["payload"] != {"plan_id": record["plan_id"], "state": record["state"]}:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, zlib.error):
                raise ReleaseRejected("STATE_INTEGRITY_FAILED") from None
        else:
            raise ReleaseRejected("STATE_INTEGRITY_FAILED")
        if record["plan_id"] != plan_id or record["schema_version"] != STORE_VERSION:
            raise ReleaseRejected("STATE_SCHEMA_MISMATCH")
        return record

    def save(self, record):
        target = self._path(record["plan_id"])
        if target.exists():
            regular(target)
        elif len(list((self.root / "plans").glob("*.json"))) >= 1000:
            raise ReleaseRejected("PLAN_COUNT_LIMIT")
        from .state_keys import protector
        provider = protector()
        plain = canonical(record)
        if len(plain) > 4 * 1024 * 1024:
            raise ReleaseRejected("STATE_RECORD_LIMIT")
        private = base64.b64encode(provider.protect(zlib.compress(plain))).decode("ascii")
        unsigned = {"payload": {"plan_id": record["plan_id"], "state": record["state"]}, "provider": provider.name, "private": private}
        envelope = canonical({**unsigned, "mac": self.mac(canonical(unsigned))})
        temporary = target.with_name(uuid.uuid4().hex + ".tmp")
        try:
            write_new(temporary, envelope)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                regular(temporary)
                temporary.unlink()

    def resolve(self, identifier):
        if not isinstance(identifier, str) or not identifier.startswith("RELEASE_"):
            self._path(identifier)
            return identifier
        suffix = identifier.removeprefix("RELEASE_")
        if len(suffix) != 24 or any(c not in "0123456789abcdef" for c in suffix):
            raise ValueError("invalid release identifier")
        paths = list((self.root / "plans").glob("*.json"))
        if len(paths) > 1000:
            raise ReleaseRejected("PLAN_COUNT_LIMIT")
        for path in paths:
            if self.load(path.stem)["binding"]["scope_id"] == suffix:
                return path.stem
        raise ValueError("unknown release")


@contextmanager
def reconstruction(store, request, seed, *, cancel=None):
    if request.get("workspace_id"):
        from .incremental import WorkspaceView, CACHE_VERSION
        with WorkspaceView(store, request["workspace_id"], cancel=cancel) as view:
            if any(c["active"] and c.get("stale") for c in view.record["corrections"]):
                raise ReleaseRejected("STALE_CORRECTIONS_REVIEW_REQUIRED")
            current = view.record["request"]
            before = {k: control_bytes(current[k]) for k in ("relations", "profile")}
            profile = load_task_profile(Path(current["profile"]))
            if any(control_bytes(current[k]) != value for k, value in before.items()):
                raise ReleaseRejected("CONTROL_CHANGED")
            plan = build_plan(view.bindings, view.graph, view.analysis, profile, seed=seed)
            if plan.selected is None:
                raise ReleaseRejected("NO_FEASIBLE_PLAN")
            chosen = plan.candidates[plan.selected]
            binding = {"inputs": [{"path": str(d.snapshot.source_path.relative_to(view.session.root)),
                        "mac": store.mac(view.session._payloads[d.snapshot.revision_id])} for d, _ in view.bindings],
                "controls": {k: store.mac(value) for k, value in before.items()},
                "artifacts": [{"name": a.filename, "size": len(a.payload), "sha256": hashlib.sha256(a.payload).hexdigest()} for a in chosen.artifacts],
                "selected": plan.selected, "scope_id": plan.scope_id, "graph_revision": view.analysis.version,
                "workspace_revision": view.record["revision"],
                "versions": [PLANNER_VERSION, VERIFIER_VERSION, PROFILE_VERSION, RULE_VERSION, ATTACK_VERSION, NORMALIZATION_VERSION, CACHE_VERSION],
                "actions": [[a.document_index, a.row, a.field_index, a.kind] for a in chosen.actions]}
            yield plan, binding
        return
    source = checked_directory(request["root"])
    if not disjoint(source, store.root):
        raise ReleaseRejected("STATE_SOURCE_OVERLAP")
    controls = {name: control_bytes(request[name]) for name in ("relations", "profile")}
    # Existing strict loaders validate structure; compare control bytes again
    # afterwards so a concurrent edit cannot substitute a different template.
    bindings = load_relation_config(Path(request["relations"]))
    profile = load_task_profile(Path(request["profile"]))
    for name, payload in controls.items():
        if control_bytes(request[name]) != payload:
            raise ReleaseRejected("CONTROL_CHANGED")
    with DocumentSession(source, cancel=cancel) as session:
        parsed, inputs = [], []
        for path, template, encoding in bindings:
            snapshot = session.capture(path)
            document = session.parse(snapshot, encoding=encoding)
            parsed.append((document, template))
            if snapshot.capture_reason:
                raise ReleaseRejected("INPUT_CAPTURE_FAILED")
            inputs.append({"path": path, "mac": store.mac(session._payloads[snapshot.revision_id])})
        with EvidenceGraph(parsed, cancel=cancel) as graph:
            analysis = graph.analyze()
            plan = build_plan(parsed, graph, analysis, profile, seed=seed)
            if plan.selected is None:
                raise ReleaseRejected("NO_FEASIBLE_PLAN")
            chosen = plan.candidates[plan.selected]
            artifacts = [{"name": a.filename, "size": len(a.payload), "sha256": hashlib.sha256(a.payload).hexdigest()} for a in chosen.artifacts]
            binding = {"inputs": inputs, "controls": {name: store.mac(payload) for name, payload in controls.items()},
                       "artifacts": artifacts, "selected": plan.selected, "scope_id": plan.scope_id, "graph_revision": analysis.version,
                       "versions": [PLANNER_VERSION, VERIFIER_VERSION, PROFILE_VERSION, RULE_VERSION, ATTACK_VERSION, NORMALIZATION_VERSION],
                       "actions": [[a.document_index, a.row, a.field_index, a.kind] for a in chosen.actions]}
            yield plan, binding


def prepare_release(root, relations, task_profile, state_dir, *, workspace_id=None, cancel=None):
    source = checked_directory(root)
    state = Path(os.path.abspath(state_dir))
    if not disjoint(source, state):
        raise ReleaseRejected("STATE_SOURCE_OVERLAP")
    store = ReleaseStore(state, create=True)
    request = {"root": str(source), "relations": str(Path(relations).absolute()), "profile": str(Path(task_profile).absolute())}
    with store.lock():
        if workspace_id:
            from .state_store import WorkspaceStore
            workspace = WorkspaceStore(store).load(workspace_id)
            if workspace["request"] != request:
                raise ReleaseRejected("WORKSPACE_REQUEST_MISMATCH")
            request["workspace_id"] = workspace_id
        seed = secrets.token_bytes(32)
        with reconstruction(store, request, seed, cancel=cancel) as (plan, binding):
            record = {"schema_version": STORE_VERSION, "plan_id": uuid.uuid4().hex, "state": "VALIDATED",
                      "request": request, "seed": seed.hex(), "binding": binding,
                      "approval_digest": store.mac(canonical({"request": request, "binding": binding})),
                      "public": public_plan(plan), "approval": None, "destination": None}
            store.save(record)
            return public_record(record)


def public_record(record):
    return {"schema_version": "d3-release-status-1", "plan_id": record["plan_id"],
            "state": record["state"], "approval_digest": record["approval_digest"],
            "release_id": "RELEASE_" + record["binding"]["scope_id"],
            "plan": record["public"], "privacy_verified": False}


def assert_binding(store, record, binding):
    if binding != record["binding"]:
        record["state"] = "NEEDS_REVIEW"
        record["approval"] = None
        store.save(record)
        raise ReleaseRejected("PLAN_INPUT_OR_POLICY_CHANGED")


@contextmanager
def current_plan(store, record, *, cancel=None):
    with ExitStack() as stack:
        try:
            plan, binding = stack.enter_context(reconstruction(store, record["request"], bytes.fromhex(record["seed"]), cancel=cancel))
            assert_binding(store, record, binding)
        except (ValueError, OSError, ProcessingStopped, KeyboardInterrupt) as exc:
            cancelled = isinstance(exc, KeyboardInterrupt) or isinstance(exc, ProcessingStopped) and exc.reason == "CANCELLED"
            record["state"], record["approval"] = "CANCELLED" if cancelled else "NEEDS_REVIEW", None
            store.save(record)
            raise
        yield plan, binding


def review_release(plan_id, state_dir, *, approve=None):
    store = ReleaseStore(state_dir)
    with store.lock():
        record = store.load(plan_id)
        workspace_id = record["request"].get("workspace_id")
        if workspace_id and record["state"] not in {"PUBLISHED", "STAGING", "RECONCILING"}:
            from .state_store import WorkspaceStore
            try:
                project = WorkspaceStore(store).load(workspace_id)
                stale = project["revision"] != record["binding"].get("workspace_revision") or project["status"] != "ready"
            except OSError:
                stale = True
            if stale:
                record["state"], record["approval"] = "NEEDS_REVIEW", None
                store.save(record)
        if approve is None:
            return public_record(record)
        if not isinstance(approve, str) or not hmac.compare_digest(approve, record["approval_digest"]):
            raise ReleaseRejected("APPROVAL_DIGEST_MISMATCH")
        if record["state"] not in {"VALIDATED", "APPROVED", "FAILED", "CANCELLED"}:
            raise ReleaseRejected("PLAN_NOT_APPROVABLE")
        with current_plan(store, record) as (plan, binding):
            verify_candidate(plan, plan.candidates[plan.selected])
        record["state"], record["approval"] = "APPROVED", approve
        store.save(record)
        return public_record(record)


def marker_bytes(record):
    return canonical({"schema_version": "d3-release-1", "release_id": "RELEASE_" + record["binding"]["scope_id"],
                      "scope": "configured_task_and_known_values", "privacy_verified": False})


def expected_files(record):
    marker = marker_bytes(record)
    return {**{a["name"]: (a["size"], a["sha256"]) for a in record["binding"]["artifacts"]},
            RELEASE_MARKER: (len(marker), hashlib.sha256(marker).hexdigest())}


def verify_tree(path, record):
    checked_directory(path)
    no_alternate_streams(path)
    expected = expected_files(record)
    if {p.name for p in path.iterdir()} != set(expected):
        raise ReleaseRejected("UNEXPECTED_OUTPUT_ENTRY")
    for name, (size, digest) in expected.items():
        file = path / name
        if regular(file).st_size != size or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ReleaseRejected("OUTPUT_BYTES_CHANGED")
        no_alternate_streams(file)


def discard_stage(store, stage, record):
    if not os.path.lexists(stage):
        return
    if stage.parent != store.root / "staging" or stage.name != record["plan_id"]:
        raise ReleaseRejected("UNSAFE_STAGE_CLEANUP")
    checked_directory(stage)
    files = list(stage.iterdir())
    if any(p.name not in expected_files(record) for p in files):
        raise ReleaseRejected("UNKNOWN_STAGE_CONTENT")
    for path in files:
        regular(path)
    for path in files:
        path.unlink()
    stage.rmdir()


def publish_rename(stage, target):
    if os.path.lexists(target):
        raise ReleaseRejected("DESTINATION_EXISTS")
    os.rename(stage, target)


def export_release(plan_id, destination, state_dir, *, cancel=None):
    store = ReleaseStore(state_dir)
    with store.lock():
        record = store.load(plan_id)
        if record["state"] != "APPROVED" or record["approval"] != record["approval_digest"]:
            raise ReleaseRejected("EXPLICIT_APPROVAL_REQUIRED")
        container = Path(os.path.abspath(destination))
        checked_directory(container.parent)
        source = checked_directory(record["request"]["root"])
        if not disjoint(container, source) or not disjoint(container, store.root):
            raise ReleaseRejected("DESTINATION_OVERLAP")
        if not container.exists():
            container.mkdir()
        container = checked_directory(container)
        if container.stat().st_dev != store.root.stat().st_dev:
            raise ReleaseRejected("SAME_VOLUME_REQUIRED")
        target = container / ("RELEASE_" + record["binding"]["scope_id"])
        if os.path.lexists(target):
            raise ReleaseRejected("DESTINATION_EXISTS")
        stage = store.root / "staging" / plan_id
        if os.path.lexists(stage):
            raise ReleaseRejected("RECOVERY_REQUIRED")
        with current_plan(store, record, cancel=cancel) as (plan, binding):
            chosen = plan.candidates[plan.selected]
            verify_candidate(plan, chosen)
            ResidualCheck(plan.protected_terms, plan.graph._budget(), strict_terms=plan.strict_terms).check(target.name)
            record["state"], record["destination"] = "STAGING", str(target)
            store.save(record)
            try:
                stage.mkdir(mode=0o700)
                for artifact in chosen.artifacts:
                    plan.graph._budget().check()
                    write_new(stage / artifact.filename, artifact.payload)
                write_new(stage / RELEASE_MARKER, marker_bytes(record))
                verify_tree(stage, record)
                # Reconstruct a second time from LIVE bytes, after disk writes.
                # Keep sources quiescent: no guarantee against a malicious
                # same-account edit in the final check/rename interval.
                with current_plan(store, record, cancel=cancel):
                    pass
                publish_rename(stage, target)
                record["state"] = "PUBLISHED"
                store.save(record)
            except (Exception, KeyboardInterrupt) as exc:
                # If rename already succeeded, retain STAGING journal for
                # recovery. Never remove or overwrite a visible release.
                if not os.path.lexists(target):
                    discard_stage(store, stage, record)
                    if record["state"] not in {"NEEDS_REVIEW", "CANCELLED"}:
                        record["state"] = "CANCELLED" if isinstance(exc, KeyboardInterrupt) or isinstance(exc, ProcessingStopped) and exc.reason == "CANCELLED" else "FAILED"
                    store.save(record)
                raise
        return public_record(record)


def recover_release(plan_id, state_dir):
    store = ReleaseStore(state_dir)
    with store.lock():
        record = store.load(plan_id)
        if record["state"] not in {"STAGING", "RECONCILING"}:
            return public_record(record)
        record["state"] = "RECONCILING"
        store.save(record)
        stage = store.root / "staging" / plan_id
        target = Path(record["destination"])
        if target.name != "RELEASE_" + record["binding"]["scope_id"]:
            raise ReleaseRejected("INVALID_RECOVERY_DESTINATION")
        if os.path.lexists(target):
            verify_tree(target, record)
            if os.path.lexists(stage):
                raise ReleaseRejected("AMBIGUOUS_RECOVERY")
            record["state"] = "PUBLISHED"
        else:
            discard_stage(store, stage, record)
            record["state"] = "APPROVED"
        store.save(record)
        return public_record(record)


def show_release(plan_id, state_dir):
    store = ReleaseStore(state_dir)
    with store.lock():
        record = store.load(store.resolve(plan_id))
        result = public_record(record)
        if record["state"] == "PUBLISHED":
            verify_tree(Path(record["destination"]), record)
            result["artifact_integrity"] = "verified_against_publication"
        return result
