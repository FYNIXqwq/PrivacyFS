"""New state assets must stay outside both new and legacy output paths."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from privacyfs.documents import DocumentSession
from privacyfs.documents.models import ProcessingStopped
from privacyfs.emit.mirror import _source_file, MirrorBuildError
from privacyfs.scanner import walk, walk_parallel, dir_size


@pytest.mark.parametrize("walker", [walk, walk_parallel])
def test_old_walk_and_hidden_sizes_exclude_private_state_without_reading_key(tmp_path, walker):
    state = tmp_path / "sensitive-state"
    nested = state / "plans"
    nested.mkdir(parents=True)
    (state / ".privacyfs-state.json").write_text("not even valid JSON", encoding="utf-8")
    (nested / "secret.txt").write_text("do not export", encoding="utf-8")
    (tmp_path / "ordinary.txt").write_text("ordinary", encoding="utf-8")
    entries = list(walker(tmp_path, [], lambda _: False))
    assert [str(e.relpath) for e in entries] == ["ordinary.txt"]
    assert not list(walker(nested, [], lambda _: False))
    assert dir_size(state) == 0
    assert dir_size(tmp_path) == len(b"ordinary")
    with pytest.raises(ProcessingStopped, match="PRIVATE_STATE"):
        DocumentSession(nested)
    with DocumentSession(tmp_path) as session:
        assert session.capture("sensitive-state/plans/secret.txt").capture_reason == "PRIVATE_STATE_SOURCE"
    with pytest.raises(MirrorBuildError, match="private state"):
        _source_file(tmp_path, Path("sensitive-state/plans/secret.txt"))


def module():
    path = Path(__file__).resolve().parents[1] / "evals/run_d3.py"
    spec = importlib.util.spec_from_file_location("d3eval", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_frozen_task_corpus_partition_and_exact_utility():
    runner = module()
    path = Path(runner.__file__).parent / "data/d3-v1.jsonl"
    cases, _ = runner.load_dataset(path)
    assert len(cases) == 60 and sum(c["split"] == "validation" for c in cases) == 18
    result = runner.run(path, "validation")
    assert not result["errors"]
    assert result["counts"]["exact_task_matches"] == 18
    assert result["counts"]["better_than_full_suppression"] == 18


def test_task_corpus_hash_rejects_unversioned_edit(tmp_path):
    runner = module()
    original = Path(runner.__file__).parent / "data/d3-v1.jsonl"
    path = tmp_path / "copy.jsonl"
    shutil.copyfile(original.with_suffix(".manifest.json"), path.with_suffix(".manifest.json"))
    path.write_bytes(original.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        runner.load_dataset(path)
