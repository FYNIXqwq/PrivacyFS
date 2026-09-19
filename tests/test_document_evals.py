"""Frozen corpus integrity and split guards; full metrics run explicitly."""
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]


def runner():
    spec = importlib.util.spec_from_file_location("d1_evaluation", ROOT / "evals/run_d1.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_d1_corpus_counts_and_group_split_integrity():
    module = runner()
    cases, manifest = module.load_dataset(ROOT / "evals/data/d1-v2.jsonl")
    assert manifest["counts"] == {"entity": 1008, "file_group": 60, "task_preservation": 20}
    assert len(cases) == 1088
    assert {c["split"] for c in cases} == {"development", "validation"}
    assert all(c.get("d1_scores_relationships", False) is False for c in cases)


def test_modified_corpus_is_not_silently_accepted(tmp_path):
    import pytest
    src = ROOT / "evals/data/d1-v2.jsonl"
    target = tmp_path / src.name
    target.write_bytes(src.read_bytes() + b"\n")
    target.with_suffix(".manifest.json").write_bytes(src.with_suffix(".manifest.json").read_bytes())
    with pytest.raises(ValueError, match="hash mismatch"):
        runner().load_dataset(target)
