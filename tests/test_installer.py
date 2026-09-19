"""Installer safety without touching any real install or user state."""
import importlib.util
import json
from pathlib import Path
import uuid

import pytest


def manager():
    path = Path(__file__).resolve().parents[1] / "packaging/manage.py"
    spec = importlib.util.spec_from_file_location("trial_manager", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_foreign_directory_and_runtime_traversal_refused(tmp_path):
    module = manager()
    root = tmp_path / "app"
    root.mkdir()
    (root / "user.txt").write_bytes(b"preserve")
    with pytest.raises(ValueError, match="foreign"):
        module.load_install(root, create=True)
    with pytest.raises(ValueError, match="unsafe"):
        module.runtime_path(root, "../data")
    assert (root / "user.txt").read_bytes() == b"preserve"


def test_uninstall_preserves_data_and_refuses_state_inside_runtime(tmp_path):
    module = manager()
    root, record = module.load_install(tmp_path / "app", create=True)
    slot = uuid.uuid4().hex
    record.update(slots=[slot], current=slot, status="installed")
    module.save_install(root, record)
    runtime = module.runtime_path(root, slot)
    runtime.mkdir()
    (root / "data" / "mapping.db").write_bytes(b"user state")
    (runtime / ".privacyfs-state.json").write_bytes(b"private")
    with pytest.raises(ValueError, match="user state"):
        module.uninstall(root)
    assert (root / "data" / "mapping.db").read_bytes() == b"user state"
    (runtime / ".privacyfs-state.json").unlink()
    assert module.uninstall(root)["status"] == "uninstalled"
    assert (root / "data" / "mapping.db").read_bytes() == b"user state"


def test_bundle_hash_mismatch_is_rejected_before_install(tmp_path):
    module = manager()
    (tmp_path / "bad.whl").write_bytes(b"corrupt")
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": "d5-offline-bundle-1", "version": "test", "files": {"bad.whl": "0" * 64}}))
    with pytest.raises(ValueError, match="integrity"):
        module.bundle_manifest(tmp_path)
