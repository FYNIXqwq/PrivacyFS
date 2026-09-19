"""Tests for the split M1 scan/detect/transform/public-result pipeline."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from privacyfs.config import Rules
from privacyfs.core import (
    build_scan_result,
    detect_entries,
    iter_mapped,
    scan,
    transform_entries,
    walk_entries,
)
from privacyfs.emit.listing import to_json
from privacyfs.pseudonym import Pseudonymizer


@pytest.fixture
def pseudo(tmp_path):
    instance = Pseudonymizer(tmp_path / "mapping.db")
    yield instance
    instance.close()


def _rules() -> Rules:
    rules = Rules(ner_engine="off")
    rules.use_ner = False
    return rules


def test_split_pipeline_matches_compatibility_api(tmp_path, pseudo):
    root = tmp_path / "资料"
    (root / "文档").mkdir(parents=True)
    (root / "文档" / "张三-住院记录.txt").write_text("x")
    (root / "照片.jpg").write_bytes(b"abc")
    rules = _rules()

    raws, dir_sizes = walk_entries(root, rules)
    detected = detect_entries(raws, rules, pseudo=pseudo, dir_sizes=dir_sizes)
    mapped = transform_entries(detected, pseudo)
    split_result = build_scan_result(root, mapped, rules, pseudo)

    compatibility_result = scan(root, rules, pseudo)

    assert to_json(split_result) == to_json(compatibility_result)
    assert mapped == iter_mapped(root, rules, pseudo)


def test_intermediate_models_keep_secrets_out_of_repr_and_public_result(
    tmp_path, pseudo
):
    secret = "张三"
    root = tmp_path / "资料"
    root.mkdir()
    (root / f"{secret}-住院记录.txt").write_text("x")
    rules = _rules()

    raws, dir_sizes = walk_entries(root, rules)
    detected = detect_entries(raws, rules, pseudo=pseudo, dir_sizes=dir_sizes)

    assert len(detected) == 1
    entry = detected[0]
    assert secret in entry.raw_path.as_posix()  # retained only for internal stages
    assert secret not in repr(entry)
    assert entry.signals
    assert all(secret not in repr(signal) for signal in entry.signals)
    assert entry.entry_id.startswith("ENTRY_")
    assert secret not in entry.entry_id

    mapped = transform_entries(detected, pseudo)
    result = build_scan_result(root, mapped, rules, pseudo)
    serialized = to_json(result)

    assert secret not in repr(mapped[0])
    assert secret not in serialized
    assert secret not in repr(asdict(result))
    assert not hasattr(result.entries[0], "raw")
    assert not hasattr(result.entries[0], "raw_path")
    assert not hasattr(result.entries[0], "signals")


def test_entry_ids_are_stable_within_namespace_and_separated_across_namespaces(
    tmp_path,
):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "普通文件.txt").write_text("x")
    rules = _rules()
    raws, dir_sizes = walk_entries(root, rules)

    first = detect_entries(
        raws, rules, dir_sizes=dir_sizes, entry_namespace="release-a"
    )
    second = detect_entries(
        raws, rules, dir_sizes=dir_sizes, entry_namespace="release-a"
    )
    other_release = detect_entries(
        raws, rules, dir_sizes=dir_sizes, entry_namespace="release-b"
    )

    assert [entry.entry_id for entry in first] == [entry.entry_id for entry in second]
    assert [entry.entry_id for entry in first] != [
        entry.entry_id for entry in other_release
    ]
