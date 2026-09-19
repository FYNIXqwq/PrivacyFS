"""Read-only safe diagnostics: no source files, state payloads or endpoints."""
from __future__ import annotations

from importlib import metadata, util
import io
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import zipfile

from .documents import DocumentSession
from .documents.containers import CONTAINER_VERSION
from .normalization import NORMALIZATION_VERSION
from .state_keys import protector
from .release import ReleaseStore, write_new
from .sample_documents import docx_bytes, pdf_bytes


def doctor(*, self_test=False, state_dir=None):
    packages = {}
    for name in ("privacyfs", "pypdf", "defusedxml", "regex", "opencc-python-reimplemented", "typer", "textual", "pyyaml"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    report = {"schema_version": "d5-diagnostics-1", "python": platform.python_version(),
              "os": platform.system(), "architecture": platform.machine(), "packages": packages,
              "container_parser": CONTAINER_VERSION, "normalization": NORMALIZATION_VERSION,
              "state_protection": protector().name, "model_calls_enabled": False,
              "optional": {"hanlp_installed": util.find_spec("hanlp") is not None, "ollama_on_path": shutil.which("ollama") is not None},
              "self_test": "not_requested", "privacy_verified": False}
    if state_dir is not None:
        try:
            store = ReleaseStore(state_dir)
            with store.lock():
                report["state"] = {"status": "readable", "plan_count": len(list((store.root / "plans").glob("*.json"))),
                    "workspace_count": len(list((store.root / "workspaces").iterdir())) if (store.root / "workspaces").exists() else 0}
        except Exception:
            report["state"] = {"status": "not_readable", "reason": "STATE_CHECK_FAILED"}
    if self_test:
        checks = {}
        try:
            with tempfile.TemporaryDirectory(prefix="privacyfs-self-test-") as temporary:
                root = Path(temporary)
                for name, payload in (("test.docx", docx_bytes(paragraphs=("id=X;status=ready",))),
                                      ("test.pdf", pdf_bytes(["id=X;status=ready"]))):
                    (root / name).write_bytes(payload)
                    with DocumentSession(root) as session:
                        document = session.parse(session.capture(name))
                        checks[name.split('.')[-1]] = document.coverage.projection_complete and "id=X" in document.text
            report["self_test"] = "passed" if all(checks.values()) and len(checks) == 2 else "failed"
        except Exception:
            report["self_test"] = "failed"
        report["self_test_checks"] = checks
    return report


def write_diagnostic_bundle(path, report):
    # Explicit allowlist result from doctor; callers cannot attach arbitrary files.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(report, ensure_ascii=False, indent=2))
        archive.writestr("README.txt", "Local technical diagnostics only. No source text, file paths, mappings, model endpoints or state payloads are included.\n")
    write_new(Path(path), buffer.getvalue())
