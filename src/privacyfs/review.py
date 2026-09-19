"""Workspace services and a local terminal reviewer; headless reports stay safe."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
import uuid

from .incremental import WorkspaceView
from .release import ReleaseStore, current_plan, prepare_release, review_release
from .state_store import WorkspaceStore
from .relations import load_relation_config
from .task_profiles import load_task_profile
from .verifier import ReleaseRejected
from .documents.models import ProcessingStopped


def refresh_workspace(workspace_id, state_dir, *, cancel=None, force=False):
    releases = ReleaseStore(state_dir)
    with releases.lock(), WorkspaceView(releases, workspace_id, cancel=cancel, force=force) as view:
        return view.public()


def require_revision(record, expected):
    if type(expected) is not int or expected != record["revision"]:
        raise ReleaseRejected("WORKSPACE_REVISION_CHANGED")


def workspace_status(workspace_id, state_dir):
    releases = ReleaseStore(state_dir)
    with releases.lock():
        record = WorkspaceStore(releases).load(workspace_id)
        return {"schema_version": "d4-workspace-report-1", "workspace_id": workspace_id,
                "revision": record["revision"], "status": record["status"], "source_checked": False,
                "risk_count": None, "privacy_verified": False,
                "corrections": [{k: c[k] for k in ("id", "action", "active", "stale")} for c in record["corrections"]]}


def list_workspaces(state_dir):
    releases = ReleaseStore(state_dir)
    with releases.lock():
        store = WorkspaceStore(releases)
        result = []
        for directory in sorted(store.root.iterdir()):
            if not (directory / "index.json").exists() and store.empty_shell(directory.name):
                result.append({"workspace_id": directory.name, "revision": 0, "status": "interrupted_init"})
            else:
                record = store.load(directory.name)
                result.append({k: record[k] for k in ("workspace_id", "revision", "status")})
        return {"workspaces": result, "source_checked": False, "privacy_verified": False}


def correct_workspace(workspace_id, state_dir, action, targets, *, expected_revision):
    releases = ReleaseStore(state_dir)
    with releases.lock(), WorkspaceView(releases, workspace_id) as view:
        record, graph = view.record, view.graph
        require_revision(record, expected_revision)
        if len(record["corrections"]) >= 1000:
            raise ReleaseRejected("CORRECTION_COUNT_LIMIT")
        if not isinstance(targets, list) or not targets or len(targets) > 30_000 or any(not isinstance(t, str) for t in targets):
            raise ValueError("invalid correction references")
        resolver = graph.resolver
        affected = set()
        normalized = []
        kwargs = {"workspace_id": resolver.workspace_id, "expected_version": resolver.version}
        if action == "confirm":
            if not set(targets) <= set(resolver.assignments.values()):
                raise ValueError("unknown entity")
            normalized = sorted(o for o, e in resolver.assignments.items() if e in targets)
            affected.update(resolver.occurrences[o].locator.document_id for o in normalized)
            resolver.confirm(targets, **kwargs)
        elif action == "split":
            entity, selected = targets[0], set(targets[1:])
            members = {o for o, e in resolver.assignments.items() if e == entity}
            if not selected or not selected < members:
                raise ValueError("invalid split occurrence selection")
            normalized = [sorted(members - selected)[0], *sorted(selected)]
            affected.update(resolver.occurrences[o].locator.document_id for o in normalized)
            resolver.split(entity, selected, **kwargs)
        elif action == "reject_entity":
            if len(targets) != 1:
                raise ValueError("one entity required")
            members = sorted(o for o, e in resolver.assignments.items() if e == targets[0])
            if not members:
                raise ValueError("unknown entity")
            normalized = [members[0]]
            affected.add(resolver.occurrences[members[0]].locator.document_id)
            resolver.reject_entity(targets[0], **kwargs)
        elif action == "reject_evidence":
            if len(targets) != 1:
                raise ValueError("one evidence reference required")
            evidence = next((e for e in view.analysis.evidence if e.evidence_id == targets[0]), None)
            if evidence is None:
                raise ValueError("unknown evidence")
            for aid in evidence.assertion_ids:
                affected.update(loc.document_id for loc in graph._raw[aid].locators)
            normalized = targets
            graph.reject_evidence(view.analysis, targets[0])
        elif action == "reject_candidate":
            if len(targets) != 1:
                raise ValueError("one candidate required")
            candidate = next((c for c in view.analysis.candidates if c.candidate_id == targets[0]), None)
            if candidate is None:
                raise ValueError("unknown candidate")
            affected.update(o.locator.document_id for oid, o in resolver.occurrences.items() if resolver.assignments[oid] in candidate.entity_ids)
            normalized = targets
            graph.reject_candidate(view.analysis, targets[0])
        else:
            raise ValueError("unsupported correction action")
        event = {"id": "CORRECTION_" + uuid.uuid4().hex, "action": action, "targets": normalized,
                 "active": True, "stale": False, "at_revision": record["revision"],
                 "anchors": [{"document_id": doc_id, "revision": record["files"][doc_id]["revision"],
                              "template": record["files"][doc_id]["template"]} for doc_id in sorted(affected)]}
        record["corrections"].append(event)
        record["revision"] += 1
        view.store.save(record)
        view.store.invalidate_plans(record)
    return refresh_workspace(workspace_id, state_dir)


def undo_workspace(workspace_id, state_dir, *, expected_revision):
    releases = ReleaseStore(state_dir)
    with releases.lock():
        store = WorkspaceStore(releases)
        record = store.load(workspace_id)
        require_revision(record, expected_revision)
        event = next((e for e in reversed(record["corrections"]) if e["active"]), None)
        if event is None:
            raise ReleaseRejected("NO_ACTIVE_CORRECTION")
        if event["action"] in {"profile", "relations"}:
            record["request"][event["action"]] = event["targets"][0]
        event["active"] = False
        record["revision"] += 1
        store.save(record)
        store.invalidate_plans(record)
    return refresh_workspace(workspace_id, state_dir)


def configure_workspace(workspace_id, state_dir, kind, file, *, expected_revision):
    path = str(Path(file).absolute())
    if kind == "profile":
        load_task_profile(path)
    elif kind == "relations":
        load_relation_config(Path(path))
    else:
        raise ValueError("unknown workspace configuration")
    releases = ReleaseStore(state_dir)
    with releases.lock():
        store = WorkspaceStore(releases)
        record = store.load(workspace_id)
        require_revision(record, expected_revision)
        if len(record["corrections"]) >= 1000:
            raise ReleaseRejected("CORRECTION_COUNT_LIMIT")
        record["corrections"].append({"id": "CORRECTION_" + uuid.uuid4().hex, "action": kind,
            "targets": [record["request"][kind], path], "anchors": [], "active": True, "stale": False,
            "at_revision": record["revision"]})
        record["request"][kind] = path
        record["revision"] += 1
        store.save(record)
        store.invalidate_plans(record)
    return refresh_workspace(workspace_id, state_dir)


def prepare_workspace(workspace_id, state_dir):
    releases = ReleaseStore(state_dir)
    with releases.lock():
        record = WorkspaceStore(releases).load(workspace_id)
        request = record["request"]
    return prepare_release(request["root"], request["relations"], request["profile"], state_dir, workspace_id=workspace_id)


def local_workspace_details(workspace_id, state_dir, *, expected_revision):
    """Private local API. Never put this object in public/headless reports."""
    releases = ReleaseStore(state_dir)
    with releases.lock(), WorkspaceView(releases, workspace_id) as view:
        require_revision(view.record, expected_revision)
        documents = {d.snapshot.document_id: d for d, _ in view.bindings}
        return {"revision": view.record["revision"],
                "sources": [{"document_id": key, "path": str(d.snapshot.source_path)} for key, d in documents.items()],
                "evidence": [{"evidence_id": e.evidence_id, "kind": e.kind,
                    "sources": [{"path": str(documents[loc.document_id].snapshot.source_path),
                                 "locator": asdict(loc), "text": documents[loc.document_id].resolve(loc)}
                                for aid in e.assertion_ids for loc in view.graph._raw[aid].locators]}
                             for e in view.analysis.evidence],
                "occurrences": [{"occurrence_id": o.occurrence_id,
                    "entity_id": view.graph.resolver.assignments[o.occurrence_id], "value": o.value,
                    "entity_type": o.entity_type.value, "namespace": o.namespace,
                    "path": str(documents[o.locator.document_id].snapshot.source_path), "locator": asdict(o.locator)}
                    for o in view.graph.resolver.occurrences.values()]}


def local_plan_details(plan_id, state_dir):
    releases = ReleaseStore(state_dir)
    with releases.lock():
        record = releases.load(plan_id)
        with current_plan(releases, record) as (plan, _):
            selected = plan.candidates[plan.selected]
            return [{"path": str(document.snapshot.source_path), "before": document.text,
                     "filename": artifact.filename, "after": artifact.payload.decode("utf-8")}
                    for (document, _), artifact in zip(plan.bindings, selected.artifacts)]


def retention_workspace(workspace_id, state_dir, *, apply=False, keep_cache=None, expected_revision=None):
    releases = ReleaseStore(state_dir)
    with releases.lock():
        store = WorkspaceStore(releases)
        record = store.load(workspace_id)
        if apply or keep_cache is not None:
            require_revision(record, expected_revision)
        if keep_cache is not None:
            if type(keep_cache) is not bool:
                raise ValueError("invalid retention preference")
            record["keep_cache"] = keep_cache
            store.save(record)
        return store.prune(record, dry_run=not apply)


def forget_workspace(workspace_id, state_dir, *, expected_revision):
    releases = ReleaseStore(state_dir)
    with releases.lock():
        store = WorkspaceStore(releases)
        directory = store.directory(workspace_id)
        if directory.is_dir() and not (directory / "index.json").exists() and store.empty_shell(workspace_id):
            from .release import checked_directory
            checked_directory(directory)
            if (directory / "cache").exists():
                (directory / "cache").rmdir()
            directory.rmdir()
            return {"workspace_id": workspace_id, "status": "forgotten", "publication_history_retained": True}
        record = store.load(workspace_id)
        require_revision(record, expected_revision)
        record["status"] = "purging"
        store.save(record)
        store.invalidate_plans(record)
        record["files"], record["components"] = {}, {}
        if (directory / "cache").exists():
            store.prune(record, dry_run=False)
        if not {p.name for p in directory.iterdir()} <= {"index.json", "cache"}:
            raise ReleaseRejected("UNKNOWN_WORKSPACE_CONTENT")
        if (directory / "cache").exists():
            (directory / "cache").rmdir()
        from .release import regular
        regular(directory / "index.json")
        (directory / "index.json").unlink()
        directory.rmdir()
        return {"workspace_id": workspace_id, "status": "forgotten", "publication_history_retained": True}


def terminal_text(value):
    # JSON quoting prevents terminal controls and user content becoming commands.
    text = json.dumps(value, ensure_ascii=False)
    for code in range(0x202A, 0x202F):
        text = text.replace(chr(code), f"\\u{code:04x}")
    for code in range(0x2066, 0x206A):
        text = text.replace(chr(code), f"\\u{code:04x}")
    return text


def run_local_console(workspace_id, state_dir, *, read=input, write=print):
    """Line-oriented local reviewer. Injection hooks are for synthetic tests."""
    report = refresh_workspace(workspace_id, state_dir)
    write("LOCAL PRIVATE REVIEW — source text remains on this terminal")
    write(terminal_text(report))
    write("Commands: refresh | sources | evidence ID | occurrences | confirm IDS | split ENTITY OCCURRENCES | reject_evidence ID | reject_candidate ID | reject_entity ID | undo | profile PATH | relations PATH | plan | approve PLAN DIGEST | quit")
    while True:
        command = read("review> ").strip()
        if command in {"quit", "q", "exit"}:
            return
        action, _, rest = command.partition(" ")
        try:
            if action == "refresh":
                report = refresh_workspace(workspace_id, state_dir)
            elif action in {"sources", "evidence", "occurrences"}:
                local = local_workspace_details(workspace_id, state_dir, expected_revision=report["revision"])
                if action == "evidence":
                    selected = [e for e in local["evidence"] if e["evidence_id"] == rest]
                else:
                    selected = local[action]
                write(terminal_text(selected))
                continue
            elif action == "undo":
                report = undo_workspace(workspace_id, state_dir, expected_revision=report["revision"])
            elif action in {"profile", "relations"}:
                report = configure_workspace(workspace_id, state_dir, action, rest, expected_revision=report["revision"])
            elif action == "plan":
                prepared = prepare_workspace(workspace_id, state_dir)
                write(terminal_text(prepared))
                for item in local_plan_details(prepared["plan_id"], state_dir):
                    write("source: " + terminal_text(item["path"]) + " -> " + item["filename"])
                    for side in ("before", "after"):
                        write(side + ":")
                        lines = item[side].splitlines()
                        for line in lines[:40]:
                            write(terminal_text(line[:512]) + (" [line clipped]" if len(line) > 512 else ""))
                        if len(lines) > 40:
                            write("[preview clipped; full values are available through the private local SDK]")
                continue
            elif action == "approve":
                plan_id, digest = rest.split()
                write(terminal_text(review_release(plan_id, state_dir, approve=digest)))
                continue
            else:
                report = correct_workspace(workspace_id, state_dir, action, rest.split(), expected_revision=report["revision"])
            write(terminal_text(report))
        except (ValueError, OSError, ProcessingStopped) as exc:
            write(getattr(exc, "reason", "LOCAL_OPERATION_REFUSED"))
            report = workspace_status(workspace_id, state_dir)
            write(terminal_text(report))
