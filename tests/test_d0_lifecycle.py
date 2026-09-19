"""Real process lifecycle and recoverable mirror transaction tests."""
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from privacyfs import core
from privacyfs.emit.mirror import MARKER, build_mirror
from privacyfs.models import MappedEntry


def _busy_worker(ready_path):
    Path(ready_path).write_text("running")
    time.sleep(30)


def test_kill_active_pool_terminates_running_processes(tmp_path):
    pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    ready = tmp_path / "ready"
    processes = []
    try:
        pool.submit(_busy_worker, str(ready))
        deadline = time.monotonic() + 12
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "worker did not start"
        processes = list(pool._processes.values())
        core._register_pool(pool)
        core.kill_active_pool()
        core.kill_active_pool()  # repeated quit/unmount is harmless
        for process in processes:
            process.join(timeout=3)
            assert not process.is_alive()
    finally:
        for process in processes or list((getattr(pool, "_processes", None) or {}).values()):
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
        pool.shutdown(wait=True, cancel_futures=True)
        core._register_pool(None)


def test_cancel_after_scan_has_started_does_not_publish(tmp_path):
    import threading
    from privacyfs.config import Rules
    from privacyfs.pseudonym import Pseudonymizer
    cancel = threading.Event()
    root = tmp_path / "source"
    root.mkdir()
    (root / "normal.txt").write_text("x")
    p = Pseudonymizer(tmp_path / "m.db")
    try:
        with pytest.raises(core.ScanCancelled):
            core.iter_mapped(root, Rules(use_ner=False), p,
                progress=lambda message: cancel.set(), cancel=cancel)
    finally:
        p.close()


def test_restart_recovers_crash_between_directory_renames(tmp_path):
    root, dst = tmp_path / "source", tmp_path / "view"
    root.mkdir()
    (root / "a.txt").write_text("OLD")
    mapped = [MappedEntry(Path("a.txt"), "a.txt", False)]
    build_mirror(root, mapped, dst, force_copy=True)
    (root / "a.txt").write_text("NEW")
    script = tmp_path / "interrupt_build.py"
    script.write_text('''
import os, sys
from pathlib import Path
from privacyfs.emit.mirror import build_mirror
from privacyfs.models import MappedEntry
real_replace = os.replace
def crash(src, dst):
    if Path(src).name.startswith('.privacyfs-build-'):
        os._exit(71)
    return real_replace(src, dst)
os.replace = crash
build_mirror(Path(sys.argv[1]), [MappedEntry(Path('a.txt'), 'a.txt', False)],
             Path(sys.argv[2]), force_copy=True, overwrite=True)
''', encoding="utf-8")
    run = subprocess.run([sys.executable, str(script), str(root), str(dst)],
                         capture_output=True, timeout=20)
    assert run.returncode == 71, run.stderr
    assert list(tmp_path.glob(".privacyfs-backup-*/a.txt"))
    # Recovery restores OLD first. No overwrite means this call must refuse
    # replacing it, providing observable proof that recovery preserved it.
    with pytest.raises(ValueError, match="already exists"):
        build_mirror(root, mapped, dst, force_copy=True)
    assert (dst / "a.txt").read_text() == "OLD"
    assert not list(tmp_path.glob(".privacyfs-transaction-*.json"))
    build_mirror(root, mapped, dst, force_copy=True, overwrite=True)
    assert (dst / "a.txt").read_text() == "NEW"
    assert json.loads((dst / MARKER).read_text())["files"] == 1


def test_d0_source_reparse_is_not_followed(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("private")
    link = root / "link"
    try:
        link.symlink_to(private, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this host")
    from privacyfs.emit.mirror import MirrorBuildError
    with pytest.raises(MirrorBuildError):
        build_mirror(root, [MappedEntry(Path("link/secret.txt"), "safe.txt", False)], tmp_path / "view")
    assert not (tmp_path / "view").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_junction_is_not_traversed_or_mirrored(tmp_path):
    from privacyfs.scanner import walk
    from privacyfs.emit.mirror import MirrorBuildError
    root, target = tmp_path / "source", tmp_path / "private"
    root.mkdir()
    target.mkdir()
    (target / "secret.txt").write_text("private")
    junction = root / "junction"
    created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
                             capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert created.returncode == 0, created.stderr
    assert list(walk(root, [], lambda name: False)) == []
    with pytest.raises(MirrorBuildError):
        build_mirror(root, [MappedEntry(Path("junction/secret.txt"), "safe.txt", False)], tmp_path / "view")
    assert (target / "secret.txt").read_text() == "private"
