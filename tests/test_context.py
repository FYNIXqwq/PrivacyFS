"""Release-level context and combination-risk tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.config import Rules, load_rules
from privacyfs.context import build_privacy_graph
from privacyfs.core import detect_entries, walk_entries
from privacyfs.detectors.dates import DateContextDetector
from privacyfs.emit.audit import analysis_to_json, render_analysis
from privacyfs.models import DetectedEntry, DetectedSignal
from privacyfs.risk import analyze_entries


def context_rules(k: int = 3) -> Rules:
    rules = Rules(ner_engine="off", context_k=k)
    rules.use_ner = False
    rules.detect_en_names = False
    rules.detect_pinyin = False
    return rules


def detect_tree(root, rules: Rules):
    raws, dir_sizes = walk_entries(root, rules, with_size=False)
    return detect_entries(raws, rules, dir_sizes=dir_sizes)


def test_date_context_detector_records_precision():
    detector = DateContextDetector()
    assert [f.category for f in detector.find("2024-06-17记录")] == ["DATE_DAY"]
    assert [f.category for f in detector.find("2024年06月记录")] == ["DATE_MONTH"]
    assert [f.category for f in detector.find("2024届")] == ["DATE_YEAR"]


def test_context_graph_records_hierarchy_and_cooccurrence(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024-06-17住院记录.txt").write_text("x")
    rules = context_rules()

    entries = detect_tree(root, rules)
    graph = build_privacy_graph(entries)

    assert len(graph.subjects) == 1
    assert len(graph.parent_edges) == 3
    categories = graph.co_occurrence[graph.subjects[0].subject_id]
    assert {"SCHOOL", "MAJOR", "DATE_DAY", "LOCATION", "MEDICAL"} <= categories


def test_unique_combination_produces_safe_risk_report(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024-06-17住院记录.txt").write_text("x")
    rules = context_rules(k=3)

    result = analyze_entries(detect_tree(root, rules), rules)
    risk_types = {risk.risk_type for risk in result.risks}

    assert "education_reidentification" in risk_types
    assert "medical_reidentification" in risk_types
    assert "precise_event_linkage" in risk_types
    assert all(risk.support == 1 for risk in result.risks)

    rendered = render_analysis(result)
    serialized = analysis_to_json(result)
    for secret in ("北京大学", "计算机", "住院", "2024-06-17", "subject-a"):
        assert secret not in rendered
        assert secret not in serialized
    assert "SUBJECT_" in serialized
    assert "ENTRY_" in serialized


def test_k_support_avoids_flagging_shared_education_fingerprint(tmp_path):
    root = tmp_path / "data"
    for name in ("subject-a", "subject-b", "subject-c"):
        leaf = root / name / "北京大学" / "计算机专业"
        leaf.mkdir(parents=True)
        (leaf / "2024年档案.txt").write_text("x")
    rules = context_rules(k=3)

    result = analyze_entries(detect_tree(root, rules), rules)

    assert result.subjects == 3
    assert not any(
        risk.risk_type == "education_reidentification" for risk in result.risks
    )


def test_explicit_entity_links_top_level_directories(tmp_path):
    root = tmp_path / "data"
    (root / "records-a").mkdir(parents=True)
    (root / "records-b").mkdir(parents=True)
    (root / "records-a" / "张三文档.txt").write_text("x")
    (root / "records-b" / "张三照片.jpg").write_text("x")
    rules = context_rules()
    rules.literal_names = ["张三"]

    result = analyze_entries(detect_tree(root, rules), rules)

    assert result.subjects == 1
    linkage = [risk for risk in result.risks if risk.risk_type == "cross_directory_linkage"]
    assert len(linkage) == 1
    assert len(linkage[0].entry_ids) == 4


def test_root_level_files_start_as_independent_subjects(tmp_path):
    root = tmp_path / "flat"
    root.mkdir()
    (root / "北京-2024-06-17.txt").write_text("x")
    (root / "住院记录.txt").write_text("x")
    (root / "某某公司-软件工程师.txt").write_text("x")
    rules = context_rules()

    result = analyze_entries(detect_tree(root, rules), rules)

    assert result.subjects == 3
    assert not result.risks


def test_person_and_name_list_signals_link_as_same_entity():
    entries = [
        DetectedEntry(
            entry_id="ENTRY_a",
            raw_path=Path("records-a/file.txt"),
            signals=[DetectedSignal("NAME_LIST", "张三", "张三", 1.0, "rules", 1)],
        ),
        DetectedEntry(
            entry_id="ENTRY_b",
            raw_path=Path("records-b/file.txt"),
            signals=[DetectedSignal("PERSON", "张三", "张三", 1.0, "ner", 1)],
        ),
    ]

    graph = build_privacy_graph(entries)

    assert len(graph.subjects) == 1
    assert graph.subjects[0].top_level_groups == {"records-a", "records-b"}


def test_traditional_school_suffix_triggers_education_risk(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大學" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024年档案.txt").write_text("x")
    rules = context_rules(k=3)

    result = analyze_entries(detect_tree(root, rules), rules)

    assert any(risk.risk_type == "education_reidentification" for risk in result.risks)


def test_medical_support_counts_are_cached_by_identifier_set(tmp_path, monkeypatch):
    import privacyfs.risk as risk_module

    root = tmp_path / "data"
    for name in ("subject-a", "subject-b", "subject-c"):
        leaf = root / name / "北京大学" / "计算机专业"
        leaf.mkdir(parents=True)
        (leaf / "2024-06-17住院记录.txt").write_text("x")
    rules = context_rules(k=3)
    calls = 0
    original = risk_module._support_counts

    def counted(subjects, categories):
        nonlocal calls
        calls += 1
        return original(subjects, categories)

    monkeypatch.setattr(risk_module, "_support_counts", counted)
    analyze_entries(detect_tree(root, rules), rules)

    # Four static hard-rule fingerprints plus one shared medical identifier set.
    assert calls == 5


def test_context_rules_are_strictly_loaded(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "context:\n"
        "  enabled: true\n"
        "  mode: audit\n"
        "  subject_scope: whole_release\n"
        "  k_anonymity: 5\n",
        encoding="utf-8",
    )
    rules = load_rules(str(path))
    assert rules.context_enabled is True
    assert rules.context_mode == "audit"
    assert rules.context_subject_scope == "whole_release"
    assert rules.context_k == 5

    path.write_text("context:\n  k_anonymity: nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        load_rules(str(path))

    path.write_text("context:\n  k_anonymty: 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown context key"):
        load_rules(str(path))


def test_analyze_cli_is_read_only_and_emits_safe_json(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    private_file = leaf / "2024-06-17住院记录.txt"
    private_file.write_text("unchanged")
    before = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
    }

    result = CliRunner().invoke(
        app,
        ["analyze", str(root), "--ner", "off", "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["risks"] >= 1
    second_result = CliRunner().invoke(
        app,
        ["analyze", str(root), "--ner", "off", "--format", "json"],
    )
    assert second_result.exit_code == 0, second_result.output
    second_payload = json.loads(second_result.stdout)
    # Report IDs are stable only within one release; cross-release stability
    # would create a new linkage channel and allow dictionary guesses.
    assert payload["risks"][0]["subjects"] != second_payload["risks"][0]["subjects"]
    assert private_file.read_text() == "unchanged"
    after = {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
    }
    assert before == after
    for secret in ("北京大学", "计算机", "住院", "2024-06-17", "subject-a"):
        assert secret not in result.stdout
