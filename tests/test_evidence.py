"""Frozen D2 evaluation and source-visibility boundary regressions."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from test_cross_file_risk import config_for_scenario, scenario


def runner():
    location = Path(__file__).resolve().parents[1] / "evals" / "run_d2.py"
    spec = importlib.util.spec_from_file_location("d2_runner", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_corpus_partitions_and_heldout_metrics():
    module = runner()
    path = Path(module.__file__).parent / "data" / "d2-v1.jsonl"
    cases, _ = module.load_dataset(path)
    assert len(cases) == 256
    assert sum(c["split"] == "validation" for c in cases) == 80
    result = module.run(path, "validation")
    assert result["errors"] == []
    assert result["link_pairs"]["precision"] >= .95
    assert result["link_pairs"]["recall"] >= .90
    for metric in result["risk_case_metrics"].values():
        assert metric["precision"] >= .95 and metric["recall"] >= .90
    assert result["counts"]["incremental_numbered_joins"] > 0


def test_frozen_corpus_rejects_silent_changes(tmp_path):
    module = runner()
    original = Path(module.__file__).parent / "data" / "d2-v1.jsonl"
    path = tmp_path / original.name
    shutil.copyfile(original.with_suffix(".manifest.json"), path.with_suffix(".manifest.json"))
    path.write_bytes(original.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        module.load_dataset(path)


def test_cli_refuses_outside_root_and_no_incomplete_success_json(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config = config_for_scenario()
    config["files"][0]["path"] = "../outside.csv"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    for name, text, _ in scenario():
        (root / name).write_bytes(text.encode("utf-8"))
    result = CliRunner().invoke(app, ["relate", str(root), "--config", str(path)])
    assert result.exit_code == 4
    public = json.loads(result.stdout)
    assert public["risk_count"] is None and public["status"] == "failed"
    assert "outside.csv" not in result.output
