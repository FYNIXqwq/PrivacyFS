"""Read-only pre-Git audit. Run from any directory; requires existing pathspec.

No Git commands, model calls, network access, deletion or state-file reads.
JSON contains relative file names and finding locations, never matched secrets.
This approximates a NEW Git index using per-directory .gitignore files; it
does not inspect an existing index, global excludes, history or arbitrary PII.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from pathspec import GitIgnoreSpec


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
        r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}|AKIA[A-Z0-9]{16})\b"
    ),
    "credential_assignment": re.compile(
        r"(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*"
        r"[\x22\x27][^\x22\x27\r\n]{8,}[\x22\x27]", re.IGNORECASE
    ),
}
BINARY_RESOURCES = {".png", ".ico"}
STATE_MARKERS = {".privacyfs-state.json", ".privacyfs-release.json", ".privacyfs-mirror"}


def is_link(path: Path) -> bool:
    return bool(path.lstat().st_file_attributes & 1024) if os.name == "nt" else path.is_symlink()


def main() -> int:
    specs = {}
    candidates = []
    findings = []
    binary_resources = []
    excluded_directories = []

    def load_spec(directory):
        if directory not in specs:
            path = directory / ".gitignore"
            if path.exists() and is_link(path):
                raise ValueError("linked_ignore_file")
            specs[directory] = GitIgnoreSpec.from_lines(
                path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
            )
        return specs[directory]

    def ignored(relative, is_directory=False):
        parts = Path(relative).parts
        for depth in range(1, len(parts) + 1):
            ignored_here = False
            item = ROOT.joinpath(*parts[:depth])
            suffix = "/" if depth < len(parts) or is_directory else ""
            for parent_depth in range(depth):
                parent = ROOT.joinpath(*parts[:parent_depth])
                match = load_spec(parent).check_file(item.relative_to(parent).as_posix() + suffix)
                if match.include is not None:
                    ignored_here = match.include
            if ignored_here:
                return True
        return False

    must_ignore = [
        ".venv/pyvenv.cfg", ".venv-test/pyvenv.cfg", ".review-deps/pkg/file.py",
        "build/lib/privacyfs/cli.py", "src/privacyfs/__pycache__/cli.pyc",
        "src/privacyfs.egg-info/PKG-INFO", ".pytest_cache/README.md",
        ".review/private-report.json", ".review/fixture/source.txt",
        "mapping.db", "mapping.db-wal", "mapping.db-shm", "mapping.db-journal",
        "cache.sqlite3", "cache.sqlite3-wal", "mapping-export.json", "mapping-export.csv",
        "review-20260919-120000.pfsreview", "nested/private.pfsreview",
        "privacyfs-names-20260919-120000.json", "report-20260919-120000.json",
        "report-20260919-120000.csv",
        "rules.yaml", "rules.local.yml", "nested/rules.example.yaml", ".env", ".env.local",
        "secret.pem", "secret.key", "local-data/arbitrary/export.json",
        "MiniCPM5-2B-Q8_0.gguf", "PrivacyFS-Browser.exe", "package.whl",
        "MiniCPM5-1B-Q4_K_M.gguf", "Qwen3.5-0.8B-IQ4_XS.gguf",
        "models/model.pth", "weights/part.bin", "checkpoints/state.json",
        "nested/model.ggml", "nested/model.safetensors", "nested/model.onnx",
        "nested/model.onnx_data", "nested/model.ckpt", "nested/model.pt",
        "nested/pytorch_model-00001-of-00002.bin", "nested/adapter_model.bin", "nested/ggml-model.bin",
        "src/privacyfs/gui/app.py", "src/privacyfs/gui/main.slint", "src/privacyfs/gui/__main__.py",
        "PrivacyFS-GUI.vbs", "tests/test_gui_theme.py", ".review/gui_smoke.py", ".review/verify_gui_delivery.py",
        "AGENTS.md", "nested/AGENTS.md", "CLAUDE.md", "CODE_REVIEW.md",
        "D0_IMPLEMENTATION_REPORT.md", "GUI_IMPLEMENTATION_REPORT.md", "DEVELOPMENT_PLAN.md",
        "IMPLEMENTATION_PLAN.md", "PROJECT_ASSESSMENT_2026-09-06.md", "MARKET_RESEARCH_2026-09-06.md",
        "REPOSITORY_HYGIENE_20260919.md", "PRIVACY_AUDIT_20260919.md", "THIRD_PARTY_AUDIT_20260919.md",
        "FILENAME_PRECISION_20260918.md", "GUI_SCALE_20260918.md", "GUI_UI_FIX_20260913.md",
        "NTFS_PREVIEW_PERFORMANCE.md",
        "pi-main/package.json", "2606.12341v1.pdf", "node_modules/package/index.js",
        "wails-browser/bin/PrivacyFS-Browser.exe", "wails-browser/wails_windows_amd64.syso",
        "wails-browser/.cache/go-build/artifact", "wails-browser/frontend/dist/main.js",
        "wails-browser/build/windows/nsis/MicrosoftEdgeWebview2Setup.exe",
    ]
    must_keep = [
        "src/privacyfs/core.py", "tests/test_pipeline.py", "pyproject.toml", "requirements-dev.lock",
        ".github/workflows/tests.yml", "rules.example.yaml",
        "examples/enterprise-sim-v1/controls/rules.synthetic.yaml", "evals/data/d1-v2.jsonl",
        "examples/d2/clients.csv", "examples/d2/relations.json", "README.md",
        "src/privacyfs/models.py", "examples/model-config.json", "docs/models.md",
        "packaging/bootstrap.pth", "tests/fixtures/sample.bin", "LICENSES/model-license.txt",
        "src/privacyfs/gui/__init__.py", "src/privacyfs/gui/backend.py", "src/privacyfs/gui/results.py",
        "src/privacyfs/gui/directory_ai.py", "src/privacyfs/gui/embedded_ai.py", "src/privacyfs/gui/mark_tools.py",
        "src/privacyfs/workbench/app.py", "tests/test_gui_backend.py", "tests/test_embedded_ai.py",
        "user_manual.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "LICENSES/Wails-MIT.txt",
        "DOCUMENTS_D1.md", "RELATIONS_D2.md", "RELEASES_D3.md", "WORKSPACES_D4.md",
        "FORMATS_D5.md", "TRIAL_D5.md", "PILOT_FEEDBACK_TEMPLATE.md", "EVALUATION_FRAMEWORK.md",
        ".review/repro_review.py", ".review/check_repository_hygiene.py",
        ".review/wails-ui-qa.mjs", ".review/wails-virtual-cdp.mjs",
        "wails-browser/build.ps1", "wails-browser/go.sum", "wails-browser/web/virtual.js",
        "wails-browser/build/windows/icon.ico", "wails-browser/build/windows/wails.exe.manifest",
        "wails-browser/build/windows/info.json", "wails-browser/build/config.yml",
    ]
    for expected, paths in ((True, must_ignore), (False, must_keep)):
        for relative in paths:
            if ignored(relative) != expected:
                findings.append({"path": relative, "kind": "ignore_boundary"})

    def visit(directory):
        relative = directory.relative_to(ROOT).as_posix()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
            if any(p.name in STATE_MARKERS for p in entries):
                findings.append({"path": relative, "kind": "private_state_directory"})
                return
            for path in entries:
                rel = path.relative_to(ROOT).as_posix()
                if path.name == ".git":
                    findings.append({"path": rel, "kind": "existing_git_metadata"})
                    continue
                if is_link(path):
                    if not ignored(rel):
                        findings.append({"path": rel, "kind": "reparse_point_not_followed"})
                    continue
                directory_entry = path.is_dir()
                if ignored(rel, directory_entry):
                    if directory_entry:
                        excluded_directories.append(rel)
                    continue
                if directory_entry:
                    visit(path)
                    continue
                size = path.stat().st_size
                candidates.append({"path": rel, "bytes": size})
                if size > 1024 * 1024:
                    findings.append({"path": rel, "kind": "over_1_MiB_review_required"})
                    continue
                if path.suffix.lower() in BINARY_RESOURCES:
                    binary_resources.append(rel)
                    continue
                try:
                    content = path.read_text(encoding="utf-8-sig")
                except UnicodeError:
                    findings.append({"path": rel, "kind": "non_utf8_review_required"})
                    continue
                for line_number, line in enumerate(content.splitlines(), 1):
                    for kind, pattern in PATTERNS.items():
                        if pattern.search(line):
                            findings.append({"path": rel, "line": line_number, "kind": kind})
        except (OSError, ValueError) as exc:
            findings.append({"path": relative, "kind": "unreadable", "error_type": type(exc).__name__})

    visit(ROOT)
    print(json.dumps({
        "boundary_checks": len(must_ignore) + len(must_keep),
        "candidate_count": len(candidates), "candidate_bytes": sum(p["bytes"] for p in candidates),
        "findings": findings, "binary_resources_not_secret_scanned": binary_resources,
        "excluded_directories": excluded_directories, "candidates": candidates,
    }, ensure_ascii=True, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
