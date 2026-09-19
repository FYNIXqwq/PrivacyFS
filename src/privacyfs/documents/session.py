"""Bounded in-memory snapshots; no plaintext temporary files or external I/O."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat

from ..paths import relative_parts
from ..scanner import norm_path, winlong, has_private_state_marker, inside_private_state
from .models import (Budget, ContentFinding, DocumentSnapshot, Lease, Limits, ParseStatus,
                     ProcessingStopped)
from .parsers import failed_document, parse_bytes

_FORMATS = {".txt": "txt", ".md": "markdown", ".markdown": "markdown", ".csv": "csv", ".docx": "docx", ".pdf": "pdf"}


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _cross_api_identity(info):
    # Windows Python 3.12 can expose creation time through path stat but
    # metadata-change time through fstat. Compare ctime only within one API.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _check_regular_path(path: Path, *, directory=False):
    info = os.stat(winlong(path), follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ProcessingStopped("REPARSE_POINT")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ProcessingStopped("NOT_REGULAR_FILE")
    return info


class DocumentSession:
    def __init__(self, root: Path, *, limits: Limits | None = None, cancel=None):
        absolute = Path(os.path.abspath(root))
        for candidate in reversed([absolute, *absolute.parents]):
            _check_regular_path(candidate, directory=True)
        self.root = absolute.resolve()
        if inside_private_state(self.root):
            raise ProcessingStopped("PRIVATE_STATE_SOURCE")
        self.limits, self.cancel = limits or Limits(), cancel
        self.workspace_id = "WORKSPACE_" + secrets.token_hex(12)
        self._secret = secrets.token_bytes(32)
        self._lease = Lease()
        self._snapshots, self._payloads, self._parsed = {}, {}, {}
        self._attempts = 0
        self._chars_held = 0
        self._blocks_held = 0

    @property
    def bytes_held(self):
        return sum(len(data) for data in self._payloads.values())

    def _snapshot(self, path, format, payload=None, reason=None, size=0):
        document_id = "DOCUMENT_" + hmac.new(self._secret, norm_path(path).encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:24]
        revision = "REVISION_" + secrets.token_hex(12)
        digest = hmac.new(self._secret, payload, hashlib.sha256).hexdigest() if payload is not None else ""
        snapshot = DocumentSnapshot(document_id, revision, self.workspace_id, format,
                                    size, path, digest, reason, self._lease)
        if payload is not None:
            self._payloads[revision] = payload
            self._snapshots[revision] = snapshot
        return snapshot

    def capture(self, relative_path: str | Path) -> DocumentSnapshot:
        self._lease.check()
        path = self.root / relative_path
        format = _FORMATS.get(path.suffix.lower(), "unsupported")
        if self._attempts >= self.limits.max_files:
            return self._snapshot(path, format, reason="FILE_COUNT_LIMIT")
        self._attempts += 1
        budget, size = Budget(self.limits, self.cancel), 0
        try:
            budget.check()
            if has_private_state_marker(self.root):
                raise ProcessingStopped("PRIVATE_STATE_SOURCE")
            parts = relative_parts(relative_path)
            current = self.root
            for component in parts[:-1]:
                current /= component
                _check_regular_path(current, directory=True)
                if has_private_state_marker(current):
                    raise ProcessingStopped("PRIVATE_STATE_SOURCE")
            path = current / parts[-1]
            before = _check_regular_path(path)
            size = before.st_size
            if not path.resolve().is_relative_to(self.root):
                raise ProcessingStopped("OUTSIDE_ROOT")
            if format == "unsupported":
                return self._snapshot(path, format, reason="UNSUPPORTED_FORMAT", size=size)
            if size > self.limits.max_file_bytes:
                raise ProcessingStopped("FILE_LIMIT")
            available = min(self.limits.max_file_bytes, self.limits.max_total_bytes - self.bytes_held)
            if size > available:
                raise ProcessingStopped("TOTAL_BYTE_LIMIT")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(winlong(path), flags)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _cross_api_identity(opened) != _cross_api_identity(before):
                    raise ProcessingStopped("SOURCE_CHANGED")
                chunks, total = [], 0
                while True:
                    budget.check()
                    chunk = stream.read(min(65536, available - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > available:
                        raise ProcessingStopped("FILE_LIMIT" if available == self.limits.max_file_bytes else "TOTAL_BYTE_LIMIT")
                    chunks.append(chunk)
                if _identity(os.fstat(stream.fileno())) != _identity(opened):
                    raise ProcessingStopped("SOURCE_CHANGED")
            if _identity(_check_regular_path(path)) != _identity(before):
                raise ProcessingStopped("SOURCE_CHANGED")
            budget.check()
            return self._snapshot(path, format, b"".join(chunks), size=total)
        except ProcessingStopped as exc:
            return self._snapshot(path, format, reason=exc.reason, size=size)
        except ValueError:
            return self._snapshot(path, format, reason="INVALID_PATH", size=size)
        except OSError:
            return self._snapshot(path, format, reason="READ_FAILED", size=size)

    def parse(self, snapshot: DocumentSnapshot, *, encoding=None):
        self._lease.check()
        if snapshot._lease is not self._lease or snapshot.workspace_id != self.workspace_id:
            raise ValueError("snapshot belongs to another session")
        try:
            Budget(self.limits, self.cancel).check()
        except ProcessingStopped as exc:
            return failed_document(snapshot, exc.reason)
        if snapshot.capture_reason:
            return failed_document(snapshot, snapshot.capture_reason,
                                   unsupported=snapshot.capture_reason == "UNSUPPORTED_FORMAT")
        if self._snapshots.get(snapshot.revision_id) is not snapshot:
            raise ValueError("snapshot has been released or replaced")
        key = (snapshot.revision_id, encoding)
        if key in self._parsed:
            return self._parsed[key]
        try:
            document = parse_bytes(snapshot, self._payloads[snapshot.revision_id],
                                   self.limits, Budget(self.limits, self.cancel), encoding=encoding)
            characters = len(document.text) + sum(len(b.text) + len(b.normalized.text) for b in document.blocks)
            if self._chars_held + characters > self.limits.max_total_chars or self._blocks_held + len(document.blocks) > self.limits.max_blocks:
                document.clear()
                return failed_document(snapshot, "TOTAL_TEXT_LIMIT")
            self._chars_held += characters
            self._blocks_held += len(document.blocks)
            self._parsed[key] = document
            return document
        except ProcessingStopped as exc:
            return failed_document(snapshot, exc.reason)
        except ValueError as exc:
            allowed = {"UNSUPPORTED_ENCODING", "ENCODING_CONFLICT", "DECODING_FAILED",
                       "BINARY_TEXT", "TEXT_LIMIT", "INVALID_CSV", "NORMALIZATION_LIMIT",
                       "GRAPHEME_LIMIT", "MAPPING_LIMIT", "EMPTY_NORMALIZATION", "BLOCK_LIMIT"}
            return failed_document(snapshot, str(exc) if str(exc) in allowed else "PARSING_FAILED")

    def adapt_path_entry(self, entry):
        """Adapt legacy path findings to locators without opening the file."""
        self._lease.check()
        Budget(self.limits, self.cancel).check()
        parts = relative_parts(entry.raw_path)
        text = "/".join(parts)
        payload = text.encode("utf-8")
        if self._attempts >= self.limits.max_files or len(payload) > self.limits.max_file_bytes or self.bytes_held + len(payload) > self.limits.max_total_bytes:
            raise ProcessingStopped("FILE_LIMIT")
        self._attempts += 1
        snapshot = self._snapshot(self.root.joinpath(*parts), "path", payload, size=len(payload))
        try:
            doc = self.parse(snapshot)
            if doc.coverage.status is not ParseStatus.COMPLETE:
                raise ProcessingStopped(doc.coverage.reason)
            found, seen = [], set()
            budget = Budget(self.limits, self.cancel)
            for signal in entry.signals:
                budget.check()
                if not 0 <= signal.component_index < len(doc.blocks):
                    raise ValueError("invalid path component index")
                block = doc.blocks[signal.component_index]
                start = 0
                while signal.surface:
                    budget.check()
                    at = block.text.find(signal.surface, start)
                    if at == -1:
                        break
                    location = doc.locate(block, at, at + len(signal.surface))
                    key = (location.start, location.end, signal.category)
                    if key not in seen:
                        if len(found) >= self.limits.max_mentions:
                            raise ProcessingStopped("FINDING_LIMIT")
                        seen.add(key)
                        found.append(ContentFinding(signal.category, location, signal.surface, "d1-path-adapter-v1"))
                    start = at + len(signal.surface)
            return doc, found
        except BaseException:
            self.release(snapshot)
            raise

    def release(self, snapshot):
        self._lease.check()
        for key in [k for k in self._parsed if k[0] == snapshot.revision_id]:
            document = self._parsed.pop(key)
            self._chars_held -= len(document.text) + sum(len(b.text) + len(b.normalized.text) for b in document.blocks)
            self._blocks_held -= len(document.blocks)
            document.clear()
        self._snapshots.pop(snapshot.revision_id, None)
        self._payloads.pop(snapshot.revision_id, None)

    def close(self):
        if self._lease.active:
            for document in self._parsed.values():
                document.clear()
            self._parsed.clear()
            self._payloads.clear()
            self._snapshots.clear()
            self._chars_held = self._blocks_held = 0
            self._secret = b""
            self._lease.active = False

    def __enter__(self):
        self._lease.check()
        return self

    def __exit__(self, *exc):
        self.close()
