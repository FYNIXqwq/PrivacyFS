"""Build a complete mirror in staging, then replace the previous view."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

from ..models import MappedEntry
from ..paths import MIRROR_MARKER, relative_parts, validate_targets
from ..scanner import norm_path, winlong, has_private_state_marker, inside_private_state
from .scrub import freeze_times, scrub_copy

MARKER = MIRROR_MARKER


@dataclass
class MirrorStats:
    dirs: int = 0
    hidden: int = 0
    linked: int = 0
    copied: int = 0
    scrubbed: int = 0
    unscrubbed: int = 0
    skipped: int = 0
    collisions: int = 0
    failed: int = 0


class MirrorBuildError(OSError):
    """A safe public failure without source paths or OS error text."""


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _marker(path: Path) -> dict | None:
    try:
        marker = path / MARKER
        if _reparse(path) or _reparse(marker) or marker.stat().st_size > 16384:
            return None
        data = json.loads(marker.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("created_by") == "privacyfs" else None
    except (OSError, ValueError, UnicodeError):
        return None


def _remove_owned_tree(path: Path, parent: Path) -> None:
    """Delete only a checked staging/backup root, never a reparse target."""
    if not path.exists():
        return
    if path.parent != parent or _reparse(path):
        raise MirrorBuildError("mirror cleanup refused an unsafe path")
    shutil.rmtree(winlong(path))


@contextmanager
def _destination_lock(path: Path):
    # OS locks are released on process death. Keep the empty file: deleting it
    # would let a second process lock a different inode on POSIX.
    if os.path.lexists(winlong(path)) and _reparse(path):
        raise MirrorBuildError("unsafe mirror lock")
    stream = open(winlong(path), "a+b")
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise MirrorBuildError("another mirror build holds the destination lock") from None
    try:
        yield
    finally:
        stream.close()


def _write_json(path: Path, data: dict) -> None:
    with open(winlong(path), "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def _old_matches(path: Path, digest: str | None) -> bool:
    if not path.is_dir() or _reparse(path):
        return False
    if digest is None:
        return not any(path.iterdir())
    if _marker(path) is None:
        return False
    return hashlib.sha256((path / MARKER).read_bytes()).hexdigest() == digest


def _recover(dst: Path, journal: Path, destination_id: str) -> None:
    """Reconcile a switch interrupted by an exception or process termination."""
    if not os.path.lexists(winlong(journal)):
        return
    try:
        if _reparse(journal) or journal.stat().st_size > 16384:
            raise ValueError()
        data = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("token"), str):
            raise ValueError()
        token = data["token"]
        if data["destination_id"] != destination_id or str(uuid.UUID(token)) != token:
            raise ValueError()
        # Reconstruct paths from a validated UUID, not arbitrary journal paths.
        stage = dst.parent / f".privacyfs-build-{token}"
        backup = dst.parent / f".privacyfs-backup-{token}"
        stage_marker = _marker(stage) if stage.exists() else None
        current = _marker(dst) if dst.exists() else None
        if current and current.get("mirror_id") == token:
            if backup.exists():
                if not _old_matches(backup, data["old_marker_digest"]):
                    raise ValueError()
                _remove_owned_tree(backup, dst.parent)
        elif not dst.exists() and backup.exists():
            if not _old_matches(backup, data["old_marker_digest"]):
                raise ValueError()
            os.replace(backup, dst)
        elif dst.exists():
            if backup.exists() or not _old_matches(dst, data["old_marker_digest"]):
                raise ValueError()
        elif data["had_destination"]:
            raise ValueError()
        if stage.exists():
            if not stage_marker or stage_marker.get("mirror_id") != token:
                raise ValueError()
            _remove_owned_tree(stage, dst.parent)
        journal.unlink()
    except (OSError, ValueError, KeyError, TypeError):
        raise MirrorBuildError("mirror recovery required; previous data retained") from None


def _source_file(root: Path, raw: Path) -> Path:
    if inside_private_state(root):
        raise MirrorBuildError("private state cannot be mirrored")
    current = root
    for component in relative_parts(raw):
        if has_private_state_marker(current):
            raise MirrorBuildError("private state cannot be mirrored")
        current /= component
        if _reparse(current):
            raise MirrorBuildError("source changed or contains a reparse point")
    if not current.resolve().is_relative_to(root):
        raise MirrorBuildError("source escaped the scan root")
    return current


def build_mirror(
    src_root: Path, mapped: list[MappedEntry], dst_root: Path,
    overwrite: bool = False, force_copy: bool = False,
    scrub_metadata: bool = False, blur_stats: bool = False,
) -> MirrorStats:
    """Publish a fully built view; failed copies never replace an old mirror.

    Hardlinks share content but are never written by this builder. A journal
    lets the next build for this destination reconcile a crash during switching.
    Foreign/ambiguous state is retained and rejected. Keep sources quiescent.
    """
    src_root = src_root.resolve()
    if os.path.lexists(winlong(dst_root)) and _reparse(dst_root):
        raise ValueError("mirror destination cannot be a reparse point")
    dst_root = dst_root.resolve()
    if dst_root == src_root or dst_root.is_relative_to(src_root):
        raise ValueError("mirror destination must not be inside the scanned tree")
    if src_root.is_relative_to(dst_root):
        raise ValueError("mirror source must not be inside the destination tree")
    validate_targets(mapped)
    force_copy = force_copy or scrub_metadata
    destination_id = hashlib.sha256(norm_path(dst_root).encode("utf-8")).hexdigest()[:24]
    dst_root.parent.mkdir(parents=True, exist_ok=True)
    journal = dst_root.parent / f".privacyfs-transaction-{destination_id}.json"
    lock = dst_root.parent / f".privacyfs-lock-{destination_id}"
    with _destination_lock(lock):
        _recover(dst_root, journal, destination_id)
        for entry in mapped:
            _source_file(src_root, entry.raw)
        had_destination = dst_root.exists()
        old_digest = None
        if had_destination:
            if not dst_root.is_dir():
                raise ValueError("mirror destination must be a directory")
            if any(dst_root.iterdir()):
                if _marker(dst_root) is None:
                    raise ValueError("destination is not empty and has no valid mirror marker")
                if not overwrite:
                    raise ValueError("mirror already exists; pass overwrite/--force to rebuild")
                old_digest = hashlib.sha256((dst_root / MARKER).read_bytes()).hexdigest()
        token = str(uuid.uuid4())
        stage = dst_root.parent / f".privacyfs-build-{token}"
        backup = dst_root.parent / f".privacyfs-backup-{token}"
        stage.mkdir(mode=0o700)
        stats = MirrorStats(collisions=sum(entry.collision for entry in mapped))
        try:
            for entry in mapped:
                target = stage.joinpath(*relative_parts(entry.clean))
                if entry.is_dir:
                    os.makedirs(winlong(target), exist_ok=True)
                    if entry.hidden:
                        stats.hidden += 1
                    else:
                        stats.dirs += 1
                    continue
                os.makedirs(winlong(target.parent), exist_ok=True)
                src = _source_file(src_root, entry.raw)
                before = src.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise MirrorBuildError("source file changed type")
                if scrub_metadata:
                    if scrub_copy(src, target):
                        stats.scrubbed += 1
                    else:
                        stats.unscrubbed += 1
                elif force_copy:
                    shutil.copy2(winlong(src), winlong(target))
                    stats.copied += 1
                else:
                    try:
                        os.link(winlong(src), winlong(target))
                        stats.linked += 1
                    except OSError:
                        shutil.copy2(winlong(src), winlong(target))
                        stats.copied += 1
                after = _source_file(src_root, entry.raw).stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns
                ):
                    raise MirrorBuildError("source changed during mirror build")
            from ..policy import bucket_count
            count = bucket_count if blur_stats else int
            _write_json(stage / MARKER, {
                "created_by": "privacyfs", "format_version": 1, "mirror_id": token,
                "time": "[REDACTED]" if blur_stats else time.strftime("%Y-%m-%dT%H:%M:%S"),
                "files": count(stats.linked + stats.copied + stats.scrubbed + stats.unscrubbed),
                "dirs": count(stats.dirs), "hidden_placeholders": count(stats.hidden),
            })
            if scrub_metadata:
                for directory, _dirs, files in os.walk(winlong(stage), topdown=False):
                    for name in files:
                        freeze_times(Path(directory) / name, strict=True)
                    freeze_times(Path(directory), strict=True)
            _write_json(journal, {
                "destination_id": destination_id, "token": token,
                "had_destination": had_destination, "old_marker_digest": old_digest,
            })
            if had_destination:
                os.replace(dst_root, backup)
            os.replace(stage, dst_root)
            _recover(dst_root, journal, destination_id)
            return stats
        except BaseException as exc:
            stats.failed += 1
            if journal.exists():
                _recover(dst_root, journal, destination_id)
            elif stage.exists():
                _remove_owned_tree(stage, dst_root.parent)
            if isinstance(exc, OSError):
                raise MirrorBuildError("mirror build failed; no incomplete view was published") from None
            raise
