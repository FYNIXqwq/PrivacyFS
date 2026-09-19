"""Detection/configuration/cache regressions for the D0 baseline."""
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app, _exclusions_for
from privacyfs.config import Rules
from privacyfs.context import build_privacy_graph
from privacyfs.core import detect_entries, sanitize_path, walk_entries
from privacyfs.detectors.context_terms import ContextTermDetector
from privacyfs.detectors.keywords import Finding, KeywordDetector
from privacyfs.detectors.llm import LLMDetector
from privacyfs.pseudonym import Pseudonymizer
from privacyfs.scanner import norm_path


def test_cli_implicit_ner_does_not_override_yaml(tmp_path, monkeypatch):
    import privacyfs.core as core
    root = tmp_path / "source"
    root.mkdir()
    (root / "普通文件.txt").write_text("x")
    rules = tmp_path / "rules.yaml"
    rules.write_text("ner_engine: 'off'\n", encoding="utf-8")
    seen = []
    original = core._detectors_for
    def observe(rule_set, *args, **kwargs):
        seen.append(rule_set.ner_engine)
        return original(rule_set, *args, **kwargs)
    monkeypatch.setattr(core, "_detectors_for", observe)
    result = CliRunner().invoke(app, ["scan", str(root), "--rules", str(rules),
        "--db", str(tmp_path / "db.sqlite"), "--no-size", "-f", "json"])
    assert result.exit_code == 0, result.output
    assert seen and set(seen) == {"off"}


def test_replacements_use_original_spans_and_report_used_aliases(tmp_path):
    p = Pseudonymizer(tmp_path / "m.db")
    try:
        clean, aliases = sanitize_path(Path("Alice-SON.txt"),
            [Finding("PERSON", "Alice"), Finding("NAME_LIST", "SON")], p)
        assert clean == "PERSON_0001-PERSON_0002.txt"
        assert all(alias in clean for alias in aliases)
        clean, aliases = sanitize_path(Path("AliceSmith.txt"),
            [Finding("PERSON", "AliceSmith"), Finding("PERSON", "Alice")], p)
        assert len(aliases) == 1 and clean == aliases[0] + ".txt"
    finally:
        p.close()


@pytest.mark.parametrize("term,name", [("visa", "VİSA.pdf"), ("i", "ı.txt"), ("K", "K.txt")])
def test_unicode_case_matching_keeps_category_and_original_surface(term, name):
    findings = KeywordDetector([(term, "NAME_LIST")]).find(name)
    assert len(findings) == 1
    assert findings[0].category == "NAME_LIST"
    assert findings[0].surface == name[:-4]


def test_explicit_org_names_do_not_merge_people(tmp_path):
    root = tmp_path / "source"
    for name in ("a", "b", "c"):
        (root / name).mkdir(parents=True)
        (root / name / "某某公司-记录.txt").write_text("x")
    rules = Rules(use_ner=False, detect_pinyin=False, detect_en_names=False,
                  literal_names=["某某公司"])
    raws, sizes = walk_entries(root, rules, with_size=False)
    entries = detect_entries(raws, rules, dir_sizes=sizes)
    assert len(build_privacy_graph(entries).subjects) == 3


def test_person_category_alias_reuses_existing_without_renumbering(tmp_path):
    p = Pseudonymizer(tmp_path / "m.db")
    try:
        first = p.alias("PERSON", "张三")
        assert p.alias("NAME_LIST", "张三") == first
        assert len(p.all_mappings()) == 1
    finally:
        p.close()


def test_legacy_conflicting_aliases_remain_unchanged(tmp_path):
    db = tmp_path / "legacy.db"
    p = Pseudonymizer(db)
    p.close()
    with sqlite3.connect(db) as conn:
        conn.executemany("INSERT INTO mapping VALUES (?, ?, ?)", [
            ("PERSON", "张三", "PERSON_0001"),
            ("NAME_LIST", "张三", "PERSON_0002"),
        ])
    p = Pseudonymizer(db)
    try:
        assert p.alias("PERSON", "张三") == "PERSON_0001"
        assert p.alias("NAME_LIST", "张三") == "PERSON_0002"
        assert len(p.all_mappings()) == 2
    finally:
        p.close()


@pytest.mark.parametrize("left,right", [("计算机专业", "計算機專業"), ("医疗-诊断", "醫療-診斷")])
def test_context_traditional_variants_keep_raw_surfaces(left, right):
    detector = ContextTermDetector()
    assert [f.category for f in detector.find(left)] == [f.category for f in detector.find(right)]
    assert all(f.surface in right for f in detector.find(right))


def test_model_change_does_not_reuse_none_from_old_model(tmp_path):
    p = Pseudonymizer(tmp_path / "m.db")
    try:
        a = LLMDetector(model="model-a", cache=p)
        with patch.object(a, "_chat", return_value="1:NONE") as chat:
            assert a.find_batch(["nickname.txt"]) == {}
            assert chat.call_count == 1
        b = LLMDetector(model="model-b", cache=p)
        with patch.object(b, "_chat", return_value="1:PERSON") as chat:
            assert "nickname.txt" in b.find_batch(["nickname.txt"])
            assert chat.call_count == 1
        with patch.object(a, "_chat", side_effect=AssertionError("cache not reused")):
            assert a.find_batch(["nickname.txt"]) == {}
    finally:
        p.close()


def test_large_cache_query_is_chunked(tmp_path):
    p = Pseudonymizer(tmp_path / "m.db")
    try:
        names = [f"n-{i}" for i in range(250001)]
        p.llm_cache_put({names[0]: "NONE", names[-1]: "PERSON"})
        assert p.llm_cache_get(names) == {names[0]: "NONE", names[-1]: "PERSON"}
    finally:
        p.close()


def test_database_sidecars_are_excluded(tmp_path):
    db = tmp_path / "mapping.db"
    exclusions = _exclusions_for(db, tmp_path)
    assert {norm_path(str(db) + suffix) for suffix in ("", "-wal", "-shm", "-journal")} <= exclusions


def test_bad_yaml_does_not_echo_secret_or_traceback(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("literal_names: [SYNTHETIC_SECRET\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["scan", str(tmp_path), "--rules", str(rules)])
    assert result.exit_code == 2
    assert "SYNTHETIC_SECRET" not in result.output
    assert "Traceback" not in result.output


def test_explicit_ner_overrides_disabled_yaml_and_no_ner_wins(tmp_path):
    from privacyfs.cli import _prepare_rules
    rules = tmp_path / "rules.yaml"
    rules.write_text("use_ner: false\nner_engine: 'off'\n", encoding="utf-8")
    args = (rules, False, False, False, False, False)
    selected = _prepare_rules(*args, ner="jieba")
    assert selected.use_ner and selected.ner_engine == "jieba"
    disabled = _prepare_rules(rules, True, False, False, False, False, ner="jieba")
    assert not disabled.use_ner and disabled.ner_engine == "off"


def test_same_model_tag_revision_invalidates_cache(tmp_path):
    p = Pseudonymizer(tmp_path / "m.db")
    try:
        old = LLMDetector(cache=p, revision="weights-1")
        with patch.object(old, "_chat", return_value="1:NONE"):
            old.find_batch(["nickname.txt"])
        new = LLMDetector(cache=p, revision="weights-2")
        with patch.object(new, "_chat", return_value="1:PERSON") as chat:
            assert new.find_batch(["nickname.txt"])
            assert chat.called
    finally:
        p.close()


def test_library_scan_excludes_database_and_sidecars(tmp_path):
    from privacyfs.core import iter_mapped
    p = Pseudonymizer(tmp_path / "m.db")
    try:
        (tmp_path / "m.db-wal").write_text("synthetic private state")
        entries = iter_mapped(tmp_path, Rules(use_ner=False), p, with_size=False)
        assert not any(e.raw.name.startswith("m.db") for e in entries)
    finally:
        p.close()


def test_bad_database_has_safe_cli_error(tmp_path):
    db = tmp_path / "SYNTHETIC_SECRET.db"
    db.write_text("not a database")
    result = CliRunner().invoke(app, ["scan", str(tmp_path), "--db", str(db), "--ner", "off"])
    assert result.exit_code == 4
    assert "SYNTHETIC_SECRET" not in result.output
    assert "Traceback" not in result.output


def test_typed_literal_org_does_not_merge_bare_company_names(tmp_path):
    root = tmp_path / "source"
    for name in ("a", "b"):
        (root / name).mkdir(parents=True)
        (root / name / "Acme-记录.txt").write_text("x")
    rules = Rules(use_ner=False, detect_pinyin=False, literal_orgs=["Acme"])
    raws, sizes = walk_entries(root, rules, with_size=False)
    entries = detect_entries(raws, rules, dir_sizes=sizes)
    assert len(build_privacy_graph(entries).subjects) == 2
    assert any(s.category == "ORG" and s.surface == "Acme" for e in entries for s in e.signals)
