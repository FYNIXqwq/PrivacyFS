"""Incremental private session and report I/O, driven by the UI timer."""
from contextlib import contextmanager
import csv
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import time
import uuid

from ..state_keys import protector
from .review_store import ReviewStore, STATES

MAGIC = b"PFSREVIEW1\n"
MAX_FRAME = 8 * 1024 * 1024


@contextmanager
def atomic_output(destination, binary=False, csv_file=False):
    destination = Path(destination).expanduser().absolute()
    if destination.exists():
        raise FileExistsError("destination exists")
    kwargs = {} if binary else {"encoding":"utf-8-sig" if csv_file else "utf-8", "newline":""}
    stream = tempfile.NamedTemporaryFile(mode="wb" if binary else "w", dir=destination.parent,
        prefix=".privacyfs-workbench-", suffix=".part", delete=False, **kwargs)
    temporary = Path(stream.name)
    try:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        if os.name == "nt":
            os.rename(temporary, destination)  # Windows refuses an existing destination.
        else:
            os.link(temporary, destination)  # Atomic no-overwrite publication on POSIX.
            temporary.unlink()
    finally:
        stream.close()
        if temporary.exists():
            temporary.unlink()  # Only the new, randomly named file owned by this operation.


def snapshot(controller, scope="all"):
    counts = controller.rows.counts()
    return {**controller.report, "schema_version":"privacyfs-workbench-report-1", "privacy_verified":False,
            "session_id":controller.session_id, "candidate_count":len(controller.rows),
            "review_counts":counts, "export_scope":scope,
            "exported_count":len(controller.rows) if scope == "all" else counts["confirmed"]}


def _encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def save_session(path, metadata, rows):
    provider = protector()
    prefix = MAGIC + (b"D" if provider.name == "windows-dpapi-user" else b"P")
    digest = hashlib.sha256(prefix)
    processed = 0
    with atomic_output(path, binary=True) as stream:
        stream.write(prefix)
        def write(value, hashed=True):
            payload = provider.protect(_encode(value))
            if len(payload) > MAX_FRAME:
                raise ValueError("session frame too large")
            packet = struct.pack("<I", len(payload)) + payload
            stream.write(packet)
            if hashed:
                digest.update(packet)
        write({"type":"metadata", "metadata":metadata})
        source = iter(rows)
        while chunk := list(itertools.islice(source, 64)):
            # Extremely long paths can require smaller frames; never truncate.
            pending = [chunk]
            while pending:
                part = pending.pop()
                if len(_encode({"type":"rows", "rows":part})) > MAX_FRAME // 2:
                    if len(part) == 1:
                        raise ValueError("session entry too large")
                    middle = len(part) // 2
                    pending.extend([part[middle:], part[:middle]])
                    continue
                write({"type":"rows", "rows":part})
                processed += len(part)
                yield processed
        write({"type":"end", "rows":processed, "sha256":digest.hexdigest()}, hashed=False)


def _validate_row(row):
    if not isinstance(row, dict):
        raise ValueError("invalid candidate")
    for key in ("path", "relative_path", "name"):
        if not isinstance(row.get(key), str) or not 0 < len(row[key]) <= 131072:
            raise ValueError("invalid candidate path")
    if type(row.get("is_dir")) is not bool or row.get("attribution") not in {"self", "parent", "context"}:
        raise ValueError("invalid candidate kind")
    signals = row.get("signals")
    if not isinstance(signals, list) or not 1 <= len(signals) <= 256:
        raise ValueError("invalid signals")
    for signal in signals:
        if not isinstance(signal, dict) or type(signal.get("is_self")) is not bool:
            raise ValueError("invalid signal")
        for key in ("category", "surface", "component", "source"):
            if not isinstance(signal.get(key), str) or len(signal[key]) > 131072:
                raise ValueError("invalid signal text")
        if "reason" in signal and (not isinstance(signal["reason"], str) or len(signal["reason"]) > 4096):
            raise ValueError("invalid reason")
    review = row.get("human_review")
    if (not isinstance(review, dict) or review.get("state") not in STATES
            or not isinstance(review.get("note"), str) or len(review["note"]) > 2000
            or review.get("updated_at") is not None and (
                not isinstance(review["updated_at"], str) or len(review["updated_at"]) > 80)):
        raise ValueError("invalid human review")


def _validate_metadata(metadata):
    if not isinstance(metadata, dict) or metadata.get("schema_version") != "privacyfs-workbench-report-1":
        raise ValueError("invalid session metadata")
    uuid.UUID(metadata["session_id"])
    if metadata.get("status") not in {"complete", "partial", "cancelled", "failed"}:
        raise ValueError("invalid scan state")
    if type(metadata.get("candidate_count")) is not int or metadata["candidate_count"] < 0:
        raise ValueError("invalid count")
    if not isinstance(metadata.get("root"), str) or not isinstance(metadata.get("settings", {}), dict):
        raise ValueError("invalid scan settings")
    if not isinstance(metadata.get("stats"), dict):
        raise ValueError("invalid scan statistics")
    for value in metadata["stats"].values():
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValueError("invalid scan statistic")
    elapsed = metadata.get("elapsed", 0)
    if type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("invalid elapsed time")
    for key in ("phase", "reason"):
        if key in metadata and (not isinstance(metadata[key], str) or len(metadata[key]) > 4096):
            raise ValueError("invalid status text")
    metadata["privacy_verified"] = False


def load_session(path, candidate, result):
    with Path(path).open("rb") as stream:
        prefix = stream.read(len(MAGIC) + 1)
        if prefix not in {MAGIC + b"D", MAGIC + b"P"}:
            raise ValueError("invalid session header")
        provider = protector("windows-dpapi-user" if prefix[-1:] == b"D" else "acl-only")
        digest, metadata = hashlib.sha256(prefix), None
        while True:
            size_bytes = stream.read(4)
            if len(size_bytes) != 4:
                raise ValueError("incomplete session")
            size = struct.unpack("<I", size_bytes)[0]
            if not 0 < size <= MAX_FRAME:
                raise ValueError("invalid frame length")
            data = stream.read(size)
            if len(data) != size:
                raise ValueError("incomplete frame")
            frame = json.loads(provider.unprotect(data))
            if not isinstance(frame, dict):
                raise ValueError("invalid frame")
            kind = frame.get("type")
            if metadata is None:
                if kind != "metadata":
                    raise ValueError("missing metadata")
                metadata = frame["metadata"]
                _validate_metadata(metadata)
            elif kind == "rows":
                rows = frame.get("rows")
                if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
                    raise ValueError("invalid row batch")
                for row in rows:
                    _validate_row(row)
                candidate.import_rows(rows)
            elif kind == "end":
                if (frame.get("sha256") != digest.hexdigest() or frame.get("rows") != len(candidate)
                        or metadata["candidate_count"] != len(candidate) or stream.read(1)):
                    raise ValueError("invalid session completion")
                result.update(metadata)
                return
            else:
                raise ValueError("invalid frame sequence")
            digest.update(size_bytes + data)
            yield len(candidate)


def csv_text(value):
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_report(path, metadata, rows, format):
    with atomic_output(path, csv_file=format == "csv") as stream:
        if format == "json":
            stream.write(json.dumps(metadata, ensure_ascii=True, allow_nan=False)[:-1] + ',"entries":[')
        elif format == "csv":
            writer = csv.writer(stream)
            writer.writerow(["path", "name", "kind", "ai_category", "ai_reason", "review_state", "review_note",
                             "scan_status", "unchecked", "export_scope"])
        else:
            raise ValueError("invalid export format")
        for index, row in enumerate(rows, 1):
            if format == "json":
                stream.write(("," if index > 1 else "") + json.dumps(row, ensure_ascii=True, allow_nan=False))
            else:
                writer.writerow([csv_text(value) for value in [row["path"], row["name"],
                    "directory" if row["is_dir"] else "file",
                    " / ".join(sorted({s["category"] for s in row["signals"]})),
                    " | ".join(s.get("reason", s["surface"]) for s in row["signals"]),
                    row["human_review"]["state"], row["human_review"]["note"], metadata["status"],
                    metadata.get("stats", {}).get("ai_unchecked", 0), metadata["export_scope"]]])
            if index % 64 == 0:
                yield index
        if format == "json":
            stream.write("]}\n")


class FileJob:
    def __init__(self, kind, iterator, candidate=None, metadata=None):
        self.kind, self.iterator = kind, iterator
        self.candidate, self.metadata = candidate, metadata
        self.processed, self.done, self.error = 0, False, ""

    def step(self):
        deadline = time.monotonic() + 0.02
        try:
            for _ in range(64):
                self.processed = next(self.iterator)
                if time.monotonic() >= deadline:
                    break
        except StopIteration:
            self.done = True
        except Exception as error:
            self.error, self.done = type(error).__name__, True
            self.cancel()

    def cancel(self):
        try:
            self.iterator.close()
        except Exception as error:
            self.error = self.error or type(error).__name__
        finally:
            if self.candidate is not None:
                self.candidate.close()
                self.candidate = None
            self.done = True
