"""Evaluation boundaries and observable metrics; all identities are synthetic."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.effectiveness.attacks import ATTACKS, AttackView, Knowledge, Table, Claims
from evals.effectiveness.metrics import score, attack_summary, compare_reports, paired_attacks
from evals.effectiveness.runner import run, load_dataset

DATA = Path(__file__).resolve().parents[1] / "evals/data/effectiveness-v1.jsonl"


def view(*, name="", background=(), history=(), state="positive"):
    return AttackView((
        Table("clients", ("cid", "name"), (("alias-a", name),)),
        Table("bridges", ("cid", "mid"), (("alias-a", "alias-m"),)),
        Table("facts", ("mid", "topic", "fact", "assertion_state"),
              (("alias-m", "synthetic-topic", "secret", state),))), background, history)


def test_exact_metrics_do_not_reward_wrong_types_duplicates_or_empty_denominators():
    assert score({("PERSON", 0, 2)}, {("ORG", 0, 2)}) == {
        "tp": 0, "fp": 1, "fn": 1, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    assert score(set(), set())["recall"] is None
    assert score(set(), set())["precision"] is None
    assert score({1}, {1})["tp"] == 1


def test_attacks_observe_only_visible_tables_and_declared_background():
    assert set(AttackView.__dataclass_fields__) == {"tables", "background", "history"}
    assert not ATTACKS["key_join"].run(view()).identities
    result = ATTACKS["key_join"].run(view(name="Synthetic Person"))
    assert result.identities == frozenset({("alias-a", "Synthetic Person")})
    assert result.attributes == frozenset({("Synthetic Person", "secret")})
    for state in ("negative", "quoted", "unknown"):
        assert not ATTACKS["key_join"].run(view(name="Synthetic Person", state=state)).attributes


def test_background_attack_abstains_on_ambiguity_and_composes_history():
    a = Knowledge("Person A", "historic-key", "")
    h = Knowledge("", "historic-key", "synthetic-topic")
    v = view(background=(a,), history=(h,))
    assert not ATTACKS["background_linkage"].run(v).identities
    assert ATTACKS["history_linkage"].run(v).identities == frozenset({("alias-a", "Person A")})
    ambiguous = view(background=(Knowledge("A", "", "synthetic-topic"),
                                 Knowledge("B", "", "synthetic-topic")))
    assert not ATTACKS["background_linkage"].run(ambiguous).identities
    repeated = view(background=(Knowledge("A", "", "synthetic-topic"),) * 2)
    assert len(ATTACKS["background_linkage"].run(repeated).identities) == 1


def test_wrong_guess_is_not_success_and_failed_run_is_not_safe():
    result = attack_summary([{"status": "complete", "identity": score({("x", "A")}, {("x", "B")}),
                              "attribute": score({("A", "secret")}, set())},
                             {"status": "failed"}, {"status": "rejected"}])
    assert result["completed"] == 1 and result["failed"] == 1 and result["rejected"] == 1
    assert result["identity"]["success_rate"] == 0
    assert result["identity"]["fp"] == 1
    assert attack_summary([{"status": "failed"}])["identity"]["success_rate"] is None


def test_paired_delta_excludes_failed_and_refused_cases():
    a = {"case": "a", "status": "complete", "identity": score({1}, {1}), "attribute": score(set(), set())}
    b = {**a, "case": "b"}
    result = paired_attacks([a, b], [{**a, "identity": score({1}, set())}, {"case": "b", "status": "rejected"}])
    assert result["paired_cases"] == 1 and result["excluded_cases"] == 1
    assert result["identity"]["delta"] == -1
    assert result["attribute"]["delta"] is None


def test_task_metric_detects_wrong_owner_even_when_aggregate_facts_match():
    from evals.effectiveness.adapters import task_score
    tables = (Table("clients", ("cid",), (("a",), ("b",))),
              Table("bridges", ("cid", "mid"), (("a", "m2"), ("b", "m1"))),
              Table("facts", ("mid", "topic", "fact", "assertion_state"),
                    (("m1", "x", "paused", "positive"), ("m2", "y", "ended", "positive"))))
    expected = [("x", "paused", "positive"), ("y", "ended", "positive")]
    relations = [("a", "x", "paused", "positive"), ("b", "y", "ended", "positive")]
    result = task_score(tables, expected, relations, {"a": "a", "b": "b"})
    assert result["facts"]["recall"] == 1
    assert result["relations"]["recall"] == 0 and not result["exact"]


def test_corpus_is_frozen_and_disjoint(tmp_path):
    cases, manifest = load_dataset(DATA)
    assert {c["suite"] for c in cases} == {"detection", "release"}
    assert manifest["synthetic"] is True
    changed = tmp_path / DATA.name
    changed.write_bytes(DATA.read_bytes() + b" ")
    changed.with_suffix(".manifest.json").write_bytes(DATA.with_suffix(".manifest.json").read_bytes())
    with pytest.raises(ValueError):
        load_dataset(changed)


def test_partition_checks_do_not_trust_only_manifest_hash(tmp_path):
    import hashlib
    cases, manifest = load_dataset(DATA)
    a = next(c for c in cases if c["suite"] == "detection" and c["split"] == "development")
    b = next(c for c in cases if c["suite"] == "detection" and c["split"] == "validation")
    b["documents"] = copy.deepcopy(a["documents"])
    payload = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases).encode()
    file = tmp_path / "tampered.jsonl"
    file.write_bytes(payload)
    manifest["sha256"] = hashlib.sha256(payload).hexdigest()
    file.with_suffix(".manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_dataset(file)


def test_real_pipeline_reports_unknown_entities_and_residual_attacks_without_gold_leak():
    report = run(DATA, "all", include_cross_file=True)
    assert report["errors"] == []
    assert report["cross_file_regression"]["counts"]["incremental_numbered_joins"] > 0
    assert report["detection"]["rules"]["overall"]["fn"] > 0
    assert report["detection"]["rules"]["overall"]["tp"] > 0
    assert report["detection"]["rules"]["overall"]["fp"] > 0
    assert report["attacks"]["privacyfs"]["key_join"]["identity"]["tp"] == 0
    assert report["attacks"]["privacyfs"]["background_linkage"]["identity"]["tp"] > 0
    assert report["attacks"]["privacyfs"]["history_linkage"]["identity"]["tp"] > 0
    assert report["utility"]["privacyfs"]["delivered_exact_rate"] == 1
    assert report["utility"]["privacyfs"]["rejected"] == 1
    assert report["utility"]["privacyfs"]["exact_rate"] == 0.9
    assert report["utility"]["suppress_all"]["exact_rate"] == 0
    text = json.dumps(report, ensure_ascii=False)
    for case in load_dataset(DATA)[0]:
        if case["suite"] == "release":
            for item in case["gold"]["subjects"]:
                assert item["identity"] not in text and item["subject"] not in text
    assert compare_reports(report, copy.deepcopy(report)) == []
    baseline = copy.deepcopy(report)
    baseline["dataset"]["sha256"] = "different"
    with pytest.raises(ValueError):
        compare_reports(report, baseline)
    baseline = copy.deepcopy(report)
    baseline["detection"]["rules"]["overall"]["recall"] = 1.0
    assert compare_reports(report, baseline)


def test_failures_do_not_become_zero_leakage_or_expose_exception_text():
    class BrokenDetector:
        version = "stub-v1"

        def predict(self, data, root):
            raise RuntimeError("PRIVATE_EXCEPTION_BODY")

    class BrokenAttack:
        version = "stub-v1"

        def run(self, public):
            raise TimeoutError("PRIVATE_ATTACK_BODY")

    report = run(DATA, "validation", detectors={"broken": BrokenDetector()}, attackers={"broken": BrokenAttack()})
    assert report["errors"]
    assert report["detection"]["broken"]["overall"]["recall"] is None
    assert report["attacks"]["raw"]["broken"]["identity"]["success_rate"] is None
    assert "PRIVATE_" not in json.dumps(report)


def test_external_roundtrip_binding_missing_and_stale_responses(tmp_path):
    from evals.effectiveness.runner import PROTOCOL_VERSION
    requests = []
    original = run(DATA, "validation", requests=requests)
    assert requests and all(set(r) == {"schema_version", "request_id", "request_sha256", "view"} for r in requests)
    assert all(set(r["view"]) == {"tables", "background", "history"} for r in requests)
    metadata = {"implementation": "test", "version": "1", "provider": "none", "model": "none", "mode": "mock"}
    data = {"schema_version": PROTOCOL_VERSION, "attacker": metadata, "responses": [
        {"request_id": r["request_id"], "request_sha256": r["request_sha256"],
         "status": "complete", "identities": [], "attributes": []} for r in requests]}
    file = tmp_path / "responses.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    replay = run(DATA, "validation", responses_path=file)
    assert replay["errors"] == []
    assert replay["external_attacker"]["mode"] == "mock"
    assert replay["utility"] == original["utility"]
    data["responses"][0]["request_sha256"] = "stale"
    data["responses"].pop()
    file.write_text(json.dumps(data), encoding="utf-8")
    failed = run(DATA, "validation", responses_path=file)
    assert {e["code"] for e in failed["errors"]} == {"ATTACK_FAILED", "ATTACK_MISSING"}


def test_cli_safe_error_and_explicit_residual_gate(tmp_path):
    root = DATA.parents[2]
    command = [sys.executable, str(root / "evals/run_effectiveness.py")]
    proc = subprocess.run(command + ["--split", "validation", "--require-no-residual-attacks"],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["gate"]["status"] == "failed"
    proc = subprocess.run(command + ["--dataset", str(tmp_path / "PRIVATE_MISSING")],
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2 and "PRIVATE_MISSING" not in proc.stdout + proc.stderr
