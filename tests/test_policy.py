"""M3 deterministic declassification and enforcement tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.config import Rules, load_rules
from privacyfs.core import build_scan_result, detect_entries, transform_entries, walk_entries
from privacyfs.emit.listing import to_json
from privacyfs.emit.mirror import MARKER
from privacyfs.models import ActionType, MappedEntry
from privacyfs.policy import (
    PolicyRejected,
    apply_operator,
    blur_mapped_structure,
    blur_scan_result,
    bucket_count,
    bucket_size,
    enforce_context_policy,
    generalize_date,
    generalize_location,
    generalize_role,
    generalize_school,
)
from privacyfs.pseudonym import Pseudonymizer


def policy_rules() -> Rules:
    rules = Rules(ner_engine="off", context_k=3)
    rules.use_ner = False
    rules.detect_en_names = False
    rules.detect_pinyin = False
    return rules


def detect_tree(root: Path, rules: Rules):
    raws, sizes = walk_entries(root, rules, with_size=True)
    return detect_entries(
        raws,
        rules,
        dir_sizes=sizes,
        entry_namespace="policy-test",
    )


def test_generic_declassification_operators():
    assert apply_operator("secret", ActionType.IDENTITY) == "secret"
    assert apply_operator("secret", ActionType.GENERALIZE, "broad") == "broad"
    assert apply_operator("secret", ActionType.SUBSTITUTE, "alias") == "alias"
    assert apply_operator("secret", ActionType.REDACT) == "[REDACTED]"
    assert apply_operator("secret", ActionType.DROP) is None
    with pytest.raises(ValueError, match="requires a replacement"):
        apply_operator("secret", ActionType.GENERALIZE)


def test_first_generalizers_and_buckets():
    assert generalize_date("2024-06-17") == "2024-06"
    assert generalize_date("2024年06月") == "2024"
    assert generalize_date("2024年") == "[DATE]"
    assert generalize_location("北京市海淀区") == "北京市"
    assert generalize_location("海淀区") == "[LOCATION]"
    assert generalize_role("高级算法工程师") == "技术人员"
    assert generalize_role("区域经理") == "管理人员"
    assert generalize_school("北京大学") == "[EDUCATION_ORG]"
    assert bucket_count(17) == "10-19"
    assert bucket_count(1200) == "1000+"
    assert bucket_size(12_843_291) == "10-100 MB"


def test_medical_context_is_suppressed_and_converges(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024-06-17住院记录.txt").write_text("secret content")
    rules = policy_rules()

    outcome = enforce_context_policy(
        detect_tree(root, rules), rules, namespace="policy-test"
    )

    assert any(risk.risk_type == "medical_reidentification" for risk in outcome.initial_risks)
    assert not any(risk.severity == "high" for risk in outcome.remaining_risks)
    assert outcome.passes >= 1
    assert any(action.category == "MEDICAL" for action in outcome.actions)
    placeholders = [
        entry for entry in outcome.entries
        if entry.display_path and "[MEDICAL_SUBTREE]" in entry.display_path.as_posix()
    ]
    assert len(placeholders) == 1
    assert placeholders[0].is_dir and placeholders[0].hidden

    pseudo = Pseudonymizer(tmp_path / "mapping.db")
    try:
        mapped = transform_entries(outcome.entries, pseudo)
        result = blur_scan_result(build_scan_result(root, mapped, rules, pseudo))
        output = to_json(result)
    finally:
        pseudo.close()
    assert "住院" not in output
    assert "2024-06-17" not in output
    assert "[MEDICAL_SUBTREE]" in output
    assert isinstance(json.loads(output)["stats"]["files"], str)
    assert all(isinstance(item["size"], str) for item in json.loads(output)["entries"])


def test_identity_triangulation_generalizes_location_and_role(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京市海淀区" / "星河公司"
    leaf.mkdir(parents=True)
    (leaf / "高级算法工程师.txt").write_text("x")
    rules = policy_rules()

    outcome = enforce_context_policy(
        detect_tree(root, rules), rules, namespace="policy-test"
    )

    assert any(risk.risk_type == "identity_triangulation" for risk in outcome.initial_risks)
    assert not any(risk.severity == "high" for risk in outcome.remaining_risks)
    display_paths = [
        (entry.display_path or entry.raw_path).as_posix()
        for entry in outcome.entries
    ]
    assert any("北京市" in path for path in display_paths)
    assert any("技术人员" in path for path in display_paths)
    assert not any("海淀区" in path or "高级算法工程师" in path for path in display_paths)


def test_sensitive_placeholder_generalizes_identifying_parent_context(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京" / "2024-06-17" / "住院记录"
    leaf.mkdir(parents=True)
    (leaf / "report.pdf").write_text("x")
    rules = policy_rules()

    outcome = enforce_context_policy(
        detect_tree(root, rules), rules, namespace="policy-test"
    )
    paths = [
        (entry.display_path or entry.raw_path).as_posix()
        for entry in outcome.entries
    ]

    assert any("[MEDICAL_SUBTREE]" in path for path in paths)
    assert not any("2024-06-17" in path or "/北京/" in path for path in paths)
    assert any("[DATE]" in path and "[LOCATION]" in path for path in paths)


def test_structure_blur_collapses_single_child_chain():
    mapped = [
        MappedEntry(Path("a"), "a", True, 0),
        MappedEntry(Path("a/b"), "a/b", True, 0),
        MappedEntry(Path("a/b/c"), "a/b/c", True, 0),
        MappedEntry(Path("a/b/c/file.txt"), "a/b/c/file.txt", False, 1),
    ]

    blurred = blur_mapped_structure(mapped)
    paths = [entry.clean for entry in blurred]

    assert paths == ["a", "a/[PATH]", "a/[PATH]/file.txt"]
    assert "a/b" not in paths and "a/b/c" not in paths


def test_enforce_rejects_when_policy_cannot_make_progress(tmp_path, monkeypatch):
    import privacyfs.policy as policy_module

    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024年档案.txt").write_text("x")
    rules = policy_rules()

    monkeypatch.setattr(
        policy_module,
        "_apply_risks",
        lambda entries, risks: (entries, [], False),
    )
    with pytest.raises(PolicyRejected) as exc:
        enforce_context_policy(
            detect_tree(root, rules), rules, namespace="policy-test"
        )
    assert exc.value.result.rejected
    assert exc.value.result.remaining_risks


def test_scan_audit_preserves_ordinary_listing(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024-06-17住院记录.txt").write_text("x")
    runner = CliRunner()

    ordinary = runner.invoke(
        app,
        [
            "scan", str(root), "-r", "0", "--ner", "off", "-f", "json",
            "--db", str(tmp_path / "ordinary.db"),
        ],
    )
    audited = runner.invoke(
        app,
        [
            "scan", str(root), "-r", "0", "--ner", "off", "-f", "json",
            "--context-mode", "audit", "--db", str(tmp_path / "audit.db"),
        ],
    )

    assert ordinary.exit_code == audited.exit_code == 0
    assert json.loads(ordinary.stdout) == json.loads(audited.stdout)
    assert "context audit" in audited.stderr


def test_scan_enforce_and_mirror_emit_only_enforced_view(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024-06-17住院记录.txt").write_text("x")
    runner = CliRunner()
    db = tmp_path / "mapping.db"

    scan_result = runner.invoke(
        app,
        [
            "scan", str(root), "-r", "0", "--ner", "off",
            "--context-mode", "enforce", "--format", "json",
            "--db", str(db),
        ],
    )
    assert scan_result.exit_code == 0, scan_result.output
    payload = json.loads(scan_result.stdout)
    assert "[MEDICAL_SUBTREE]" in scan_result.stdout
    assert "住院" not in scan_result.stdout
    assert "2024-06-17" not in scan_result.stdout
    assert isinstance(payload["stats"]["files"], str)

    dest = tmp_path / "mirror"
    mirror_result = runner.invoke(
        app,
        [
            "mirror", str(root), str(dest), "--ner", "off",
            "--context-mode", "enforce", "--copy", "--db", str(db),
        ],
    )
    assert mirror_result.exit_code == 0, mirror_result.output
    paths = [path.relative_to(dest).as_posix() for path in dest.rglob("*")]
    assert any("[MEDICAL_SUBTREE]" in path for path in paths)
    assert not any("住院" in path or "2024-06-17" in path for path in paths)
    placeholder = next(path for path in dest.rglob("*") if path.name == "[MEDICAL_SUBTREE]")
    assert placeholder.is_dir()
    assert list(placeholder.iterdir()) == []  # sensitive source bytes were not copied
    marker = json.loads((dest / MARKER).read_text(encoding="utf-8"))
    assert marker["time"] == "[REDACTED]"
    assert isinstance(marker["files"], str)


def test_cli_rejects_before_creating_output_when_policy_stalls(tmp_path, monkeypatch):
    import privacyfs.policy as policy_module

    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024年档案.txt").write_text("x")
    monkeypatch.setattr(
        policy_module,
        "_apply_risks",
        lambda entries, risks: (entries, [], False),
    )
    output = tmp_path / "unsafe.json"
    dest = tmp_path / "unsafe-mirror"
    runner = CliRunner()

    scan_result = runner.invoke(
        app,
        [
            "scan", str(root), "-r", "0", "--ner", "off",
            "--context-mode", "enforce", "-f", "json", "-o", str(output),
            "--db", str(tmp_path / "scan.db"),
        ],
    )
    mirror_result = runner.invoke(
        app,
        [
            "mirror", str(root), str(dest), "--ner", "off",
            "--context-mode", "enforce", "--copy",
            "--db", str(tmp_path / "mirror.db"),
        ],
    )

    assert scan_result.exit_code == 3
    assert mirror_result.exit_code == 3
    assert not output.exists()
    assert not dest.exists()
    assert "北京大学" not in scan_result.output
    assert "北京大学" not in mirror_result.output


def test_hidden_sibling_placeholders_are_disambiguated(tmp_path):
    root = tmp_path / "data"
    (root / "R18").mkdir(parents=True)
    (root / "NSFW").mkdir()
    rules = policy_rules()
    pseudo = Pseudonymizer(tmp_path / "hidden.db")
    try:
        mapped = transform_entries(detect_tree(root, rules), pseudo)
    finally:
        pseudo.close()

    placeholders = [entry.clean for entry in mapped if entry.hidden]
    assert len(placeholders) == 2
    assert len(set(placeholders)) == 2
    assert "[HIDDEN]" in placeholders


def test_blur_stats_is_independent_from_structure_blur(tmp_path):
    root = tmp_path / "data"
    leaf = root / "subject-a" / "北京大学" / "计算机专业"
    leaf.mkdir(parents=True)
    (leaf / "2024-06-17住院记录.txt").write_text("x")
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "context:\n"
        "  blur_structure: false\n"
        "  blur_stats: true\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "scan", str(root), "-r", "0", "--ner", "off",
            "--context-mode", "enforce", "--rules", str(rules_file),
            "--format", "json", "--db", str(tmp_path / "stats.db"),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload["stats"]["files"], str)
    assert all(isinstance(entry["size"], str) for entry in payload["entries"])
    assert "[PATH]" not in result.stdout


def test_m3_context_configuration_is_strict(tmp_path):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "context:\n"
        "  mode: enforce\n"
        "  max_policy_passes: 6\n"
        "  fail_closed: true\n"
        "  blur_structure: false\n"
        "  blur_stats: true\n",
        encoding="utf-8",
    )
    rules = load_rules(str(rules_file))
    assert rules.context_mode == "enforce"
    assert rules.context_max_policy_passes == 6
    assert rules.context_fail_closed is True
    assert rules.context_blur_structure is False
    assert rules.context_blur_stats is True

    rules_file.write_text(
        "context:\n  max_policy_passes: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="positive integer"):
        load_rules(str(rules_file))
