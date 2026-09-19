"""Offline Windows trial installer. Runtimes are replaceable; data is retained."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager

MARKER = ".privacyfs-install.json"


def checked(path, *, file=False):
    path = Path(os.path.abspath(path))
    for item in reversed([path, *path.parents]):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("reparse installation path refused")
        if item == path and file:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("shared or non-regular installation file")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("installation path is not a directory")
    return path.resolve()


def atomic_write(path, payload):
    if path.exists():
        checked(path, file=True)
    temp = path.with_name(uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def bundle_manifest(bundle):
    checked(bundle)
    data = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if data["schema_version"] != "d5-offline-bundle-1":
        raise ValueError("unknown bundle schema")
    for name, digest in data["files"].items():
        parts = PurePosixPath(name).parts
        if not parts or any(p in {".", ".."} or ":" in p or "\\" in p for p in parts) or name.startswith("/"):
            raise ValueError("invalid bundle member")
        path = checked(bundle.joinpath(*parts), file=True)
        if not path.is_relative_to(bundle.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("bundle integrity check failed")
    return data


def run(args, *, timeout=180):
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["PIP_CONFIG_FILE"] = os.devnull
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env=environment,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("installation subprocess failed")
    return result.stdout


def probe(python):
    result = json.loads(run([str(python), "-c", "import sys,struct,json;print(json.dumps([sys.platform,list(sys.version_info[:2]),struct.calcsize('P')]))"]))
    if result[0] != "win32" or result[1] not in [[3, 10], [3, 11], [3, 12]] or result[2] != 8:
        raise ValueError("Windows x64 Python 3.10, 3.11 or 3.12 is required")
    return result[1]


def load_install(root, *, create=False):
    root = Path(os.path.abspath(root))
    if not root.exists():
        if not create:
            raise ValueError("installation does not exist")
        checked(root.parent)
        root.mkdir()
    checked(root)
    marker = root / MARKER
    if not marker.exists():
        if not create or any(root.iterdir()):
            raise ValueError("foreign installation directory refused")
        data = {"schema_version": "d5-install-1", "slots": [], "current": None, "previous": None, "status": "empty"}
        (root / "runtimes").mkdir()
        (root / "data").mkdir()
        save_install(root, data)
    else:
        checked(marker, file=True)
        data = json.loads(marker.read_text(encoding="utf-8"))
    if data["schema_version"] != "d5-install-1" or not isinstance(data["slots"], list):
        raise ValueError("invalid installation marker")
    for token in [*data["slots"], data["current"], data["previous"]]:
        if token is not None and not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("invalid runtime slot")
    return root, data


@contextmanager
def installation_lock(root):
    file = root / ".install.lock"
    if file.exists():
        checked(file, file=True)
    stream = file.open("a+b")
    try:
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        yield
    finally:
        stream.close()


def reconcile_pointer(root):
    _, data = load_install(root)
    pointer = root / "current.txt"
    if not pointer.exists() or data["status"] == "removing":
        return
    checked(pointer, file=True)
    slot = pointer.read_text(encoding="ascii")
    if slot not in data["slots"]:
        raise ValueError("unknown current runtime")
    if slot != data["current"]:
        health = json.loads(run([str(runtime_path(root, slot) / "Scripts/privacyfs.exe"), "doctor", "--self-test"]))
        if health["self_test"] != "passed":
            raise RuntimeError("pending runtime health check failed")
        data.update(previous=data["current"], current=slot, status="installed", version=health["packages"]["privacyfs"])
        save_install(root, data)


def save_install(root, data):
    atomic_write(root / MARKER, json.dumps(data, sort_keys=True).encode())


def runtime_path(root, slot):
    path = root / "runtimes" / slot
    if not re.fullmatch(r"[0-9a-f]{32}", slot) or not path.resolve().is_relative_to((root / "runtimes").resolve()):
        raise ValueError("unsafe runtime target")
    return path


def safe_remove_runtime(root, slot):
    path = runtime_path(root, slot)
    if not path.exists():
        return
    checked(path)
    # Never remove user state or publications accidentally placed in a runtime.
    protected = {".privacyfs-state.json", ".privacyfs-release.json", ".privacyfs-mirror"}
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in dirs:
            checked(Path(current) / name)
        for name in files:
            file = Path(current) / name
            checked(file, file=True)
            if name in protected or file.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                raise ValueError("user state found inside managed runtime; retained")
    # Both the absolute target and every reparse boundary were checked above.
    shutil.rmtree(path)


def activate(root, slot):
    checked(runtime_path(root, slot))
    atomic_write(root / "current.txt", slot.encode("ascii"))
    launcher = '@echo off\r\nsetlocal\r\nset PYTHONPATH=\r\nset PYTHONHOME=\r\nset /p PRIVACYFS_SLOT=<"%~dp0current.txt"\r\n"%~dp0runtimes\\%PRIVACYFS_SLOT%\\Scripts\\privacyfs.exe" %*\r\n'
    atomic_write(root / "privacyfs.cmd", launcher.encode("ascii"))


def _install(bundle, target, python):
    manifest = bundle_manifest(bundle)
    version = probe(python)
    root, data = load_install(target, create=True)
    slot = uuid.uuid4().hex
    destination = runtime_path(root, slot)
    # Persist ownership before creating the runtime; failed installs can be
    # cleaned by uninstall, while the current working slot remains unchanged.
    data["slots"].append(slot)
    save_install(root, data)
    try:
        run([str(python), "-m", "venv", str(destination)])
        interpreter = destination / "Scripts/python.exe"
        run([str(interpreter), "-m", "pip", "install", "--no-index", "--no-cache-dir", "--disable-pip-version-check",
             "--require-hashes", "--find-links", str(bundle / "wheelhouse"), "-r", str(bundle / "runtime-requirements.txt")])
        run([str(interpreter), "-m", "pip", "check"])
        health = json.loads(run([str(destination / "Scripts/privacyfs.exe"), "doctor", "--self-test"]))
        if health["self_test"] != "passed" or health["packages"]["privacyfs"] != manifest["version"]:
            raise RuntimeError("installed application health check failed")
    except Exception:
        safe_remove_runtime(root, slot)
        data["slots"].remove(slot)
        save_install(root, data)
        raise
    previous = data["current"]
    # Each slot is complete before any launcher points to it. If interrupted
    # between pointer and manifest, both candidate runtimes remain intact.
    activate(root, slot)
    data.update(current=slot, previous=previous, status="installed", version=manifest["version"])
    save_install(root, data)
    return {"status": "installed", "version": manifest["version"], "python": version, "slot": slot, "data_retained": True}


def _rollback(target):
    root, data = load_install(target)
    previous = data["previous"]
    if previous is None:
        raise ValueError("no previous runtime")
    health = json.loads(run([str(runtime_path(root, previous) / "Scripts/privacyfs.exe"), "doctor", "--self-test"]))
    if health["self_test"] != "passed":
        raise RuntimeError("previous runtime health check failed")
    activate(root, previous)
    data["current"], data["previous"] = previous, data["current"]
    data["version"] = health["packages"]["privacyfs"]
    save_install(root, data)
    return {"status": "rolled_back", "version": data["version"], "data_retained": True}


def _uninstall(target):
    root, data = load_install(target)
    data["status"] = "removing"
    save_install(root, data)
    for slot in sorted(data["slots"], key=lambda s: s == data["current"]):
        safe_remove_runtime(root, slot)
    for name in ("privacyfs.cmd", "current.txt"):
        path = root / name
        if path.exists():
            checked(path, file=True)
            path.unlink()
    data.update(slots=[], current=None, previous=None, status="uninstalled")
    save_install(root, data)
    return {"status": "uninstalled", "data_retained": True}


def install(bundle, target, python):
    bundle_manifest(bundle)
    probe(python)
    root, _ = load_install(target, create=True)
    with installation_lock(root):
        reconcile_pointer(root)
        return _install(bundle, target, python)


def rollback(target):
    root, _ = load_install(target)
    with installation_lock(root):
        reconcile_pointer(root)
        return _rollback(target)


def uninstall(target):
    root, _ = load_install(target)
    with installation_lock(root):
        return _uninstall(target)


def main():
    parser = argparse.ArgumentParser(description="PrivacyFS offline Windows trial manager")
    parser.add_argument("action", choices=("install", "rollback", "uninstall"))
    parser.add_argument("target", type=Path)
    parser.add_argument("--python-exe", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    try:
        if args.action == "install":
            result = install(Path(__file__).resolve().parent, args.target, args.python_exe)
        elif args.action == "rollback":
            result = rollback(args.target)
        else:
            result = uninstall(args.target)
        print(json.dumps(result))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = str(exc) if type(exc) in {ValueError, RuntimeError} else "filesystem, process or package operation failed"
        print(json.dumps({"status": "failed", "reason": "INSTALLATION_OPERATION_FAILED", "detail": detail, "data_retained": True}))
        raise SystemExit(4)


if __name__ == "__main__":
    main()
