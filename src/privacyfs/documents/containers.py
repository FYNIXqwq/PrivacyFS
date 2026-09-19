"""Killable resource-bounded parsing, then source-mapped selected text views."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

from ..source_map import OffsetMap
from .models import DocumentIR, ParseCoverage, ParseStatus, ProcessingStopped
from .parsers import _make_block, failed_document

CONTAINER_VERSION = "d5-docx-pdf-text-v2"
_ERRORS = {"INVALID_DOCX", "DOCX_PACKAGE_LIMIT", "DOCX_EXPANSION_LIMIT", "INVALID_DOCX_PACKAGE_PATH",
           "INVALID_DOCX_RELATIONSHIPS", "INVALID_DOCX_MAIN_PART", "UNSUPPORTED_DOCX_XML", "MACRO_PACKAGE_UNSUPPORTED",
           "CONTAINER_TEXT_LIMIT", "PDF_ENCRYPTED", "PDF_PAGE_LIMIT", "PDF_STREAM_LIMIT", "PDF_NO_TEXT",
           "CONTAINER_TIME_LIMIT", "CONTAINER_MEMORY_LIMIT", "CONTAINER_RESULT_LIMIT", "CONTAINER_PARSE_FAILED", "DOCX_TABLE_LIMIT", "FILE_LIMIT"}


class WindowsJob:
    def __init__(self, process, memory_limit):
        import ctypes
        from ctypes import wintypes as W
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong), ("flags", W.DWORD),
                        ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t), ("active", W.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ro", "wo", "oo", "rb", "wb", "ob")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = W.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
        self.kernel.SetInformationJobObject.restype = W.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = W.BOOL
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x100 | 0x2000 | 0x8 | 0x400  # memory, kill-on-close, one process, no fault dialog
        limits.basic.active = 1
        limits.process_memory = memory_limit
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise ProcessingStopped("CONTAINER_RESOURCE_GUARD_FAILED")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_worker(kind, payload, limits, budget):
    budget.check()
    # Windows venv launchers may spawn another process before job assignment.
    # Start the matching base interpreter directly and give it this installation's
    # import paths. No document bytes are sent until the resource guard is set.
    executable = getattr(sys, "_base_executable", sys.executable)
    paths = [str(Path(__file__).resolve().parents[2]), *sys.path]
    bootstrap = "import json,sys,runpy;sys.path[:]=json.loads(sys.argv.pop(1));runpy.run_module('privacyfs.documents.container_worker',run_name='__main__')"
    process = subprocess.Popen([executable, "-S", "-c", bootstrap, json.dumps(paths), kind, json.dumps(asdict(limits))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    guard = None
    try:
        if os.name == "nt":
            guard = WindowsJob(process, limits.max_worker_memory_bytes)
        sent = False
        while True:
            budget.check()
            try:
                output, _ = process.communicate(payload if not sent else None, timeout=min(0.1, budget.remaining()))
                break
            except subprocess.TimeoutExpired:
                sent = True
        if process.returncode != 0 or len(output) > 32 * 1024 * 1024:
            raise ProcessingStopped("CONTAINER_WORKER_FAILED")
        try:
            result = json.loads(output.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ProcessingStopped("CONTAINER_WORKER_FAILED") from None
        budget.check()
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if guard:
            guard.close()


def parse_container(snapshot, payload, limits, budget):
    result = run_worker(snapshot.format, payload, limits, budget)
    if "error" in result:
        reason = result["error"] if result["error"] in _ERRORS else "CONTAINER_PARSE_FAILED"
        return failed_document(snapshot, reason, unsupported=reason in {"PDF_ENCRYPTED", "PDF_NO_TEXT", "UNSUPPORTED_DOCX_XML", "MACRO_PACKAGE_UNSUPPORTED"})
    units = result["units"]
    if len(units) > limits.max_blocks or sum(len(u["text"]) + 1 for u in units) > limits.max_text_chars:
        raise ProcessingStopped("CONTAINER_TEXT_LIMIT")
    text, blocks, offset = [], [], 0
    for index, unit in enumerate(units):
        budget.check()
        value = unit["text"]
        mapping = OffsetMap()
        if value:
            mapping.append(len(value), offset, offset + len(value))
        block = _make_block(snapshot, index, value, mapping, limits, budget, kind=unit["kind"],
                            row=unit.get("row"), column=unit.get("column"))
        for name in ("part", "page", "paragraph", "table"):
            setattr(block, name, unit.get(name))
        blocks.append(block)
        text.append(value + "\n")
        offset += len(value) + 1
    joined = "".join(text)
    coverage = ParseCoverage(ParseStatus.PARTIAL, "CONTAINER_PROJECTION_ONLY", len(joined), len(joined),
        result["scope"], CONTAINER_VERSION, tuple(result["unprocessed"]), result["projection_complete"], result["total_parts"])
    return DocumentIR(snapshot, coverage, joined, blocks, _lease=snapshot._lease,
                      _limits=limits, _cancel=budget.cancel)
