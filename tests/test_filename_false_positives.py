"""Filename precision regressions, paired with genuine sensitive-name checks."""
import threading

import pytest

from privacyfs.config import Rules
from privacyfs.core import _detectors_for
from privacyfs.detectors.pinyin import PinyinDetector
from privacyfs.gui.backend import ScanOptions, scan


NORMAL_NAMES = [
    "LICENSE", "License.txt", "license.md", "LICENCE.rst", "LICENSE-MIT",
    "LICENSE-APACHE", "MIT License.txt", "Apache License.txt", "LicenseMIT",
    "LicenseApache", "license_mit", "apache_license", "LICENSE-APACHE-2.0",
    "cache", "module.py", "manage.py", "machine.json", "line.py", "mode.json",
    "pane.ui", "rename.py", "same.txt", "sane.json", "shine.png", "manga", "lineman.txt",
    "cookie.json", "cookies.sqlite", "coordinator.py", "cooperate.txt", "coffers.txt",
    "COOKIE.JSON", "ＣＯＯＫＩＥ.json", "offering.txt", "resumed.json", "visage.png",
]


@pytest.fixture(scope="module")
def detectors():
    return _detectors_for(Rules(use_ner=False, use_llm=False))


@pytest.mark.parametrize("name", NORMAL_NAMES)
def test_ordinary_software_and_english_names_are_not_personal_data(name, detectors):
    assert [(type(d).__name__, f.category, f.surface) for d in detectors for f in d.find(name)] == []


@pytest.mark.parametrize("name,surface", [
    ("LICENSE-zhangsan.txt", "zhangsan"), ("LICENSE-Li-Si.txt", "Li-Si"),
    ("license-ZhangSan.txt", "ZhangSan"), ("John Smith-LICENSE.txt", "John Smith"),
    ("LICENSE-张三.txt", "张三"), ("LICENSE-13812345678.txt", "13812345678"),
    ("LICENSE-user@example.com", "LICENSE-user@example.com"),
    ("COO_salary.csv", "COO"), ("visa-2026.pdf", "visa"),
    ("个人VİSA.pdf", "VİSA"), ("ＣＯＯ_任命.txt", "ＣＯＯ"),
    ("offer.pdf", "offer"), ("resume.docx", "resume"),
])
def test_normal_tokens_do_not_exempt_other_sensitive_parts(name, surface, detectors):
    assert surface in {f.surface for d in detectors for f in d.find(name)}


@pytest.mark.parametrize("name", ["Li Ne.txt", "LiNe.txt", "Li Cen Se.txt", "LiCenSe.txt", "Mo De.txt"])
def test_explicitly_separated_or_camel_pinyin_stays_detectable(name):
    assert PinyinDetector().find(name)


def test_explicit_rules_override_english_heuristic_exemptions():
    dets = _detectors_for(Rules(use_ner=False, literal_names=["License"], literal_orgs=["Apache"],
                               keywords=["cookie"]))
    assert ("NAME_LIST", "License") in {(f.category, f.surface) for d in dets for f in d.find("License-MIT")}
    assert ("ORG", "Apache") in {(f.category, f.surface) for d in dets for f in d.find("Apache-LICENSE")}
    assert ("KEYWORD", "cookie") in {(f.category, f.surface) for d in dets for f in d.find("mycookie.json")}


def test_gui_scans_licenses_and_their_sensitive_descendants(tmp_path):
    (tmp_path / "LICENSE-MIT").write_text("synthetic text", encoding="utf-8")
    (tmp_path / "LICENSE-APACHE").mkdir()
    (tmp_path / "LICENSE-APACHE" / "张三-13812345678.txt").touch()
    (tmp_path / "LICENSE-APACHE" / "ordinary.txt").touch()
    events = []
    scan(ScanOptions(str(tmp_path), include_parents=True), threading.Event(), events.append)
    assert events[-1]["status"] == "complete"
    assert events[-1]["stats"]["checked"] == 4
    hits = [row for e in events if e["kind"] == "hits" for row in e["rows"]]
    assert [r["name"] for r in hits] == ["张三-13812345678.txt"]
    assert all(s["is_self"] for s in hits[0]["signals"])


def test_parallel_worker_uses_same_precision_rules():
    from privacyfs.parallel import init_worker, detect_chunk
    init_worker(Rules(use_ner=False))
    assert detect_chunk(NORMAL_NAMES) == {}
    found = detect_chunk(["LICENSE-zhangsan.txt", "COO_salary.csv"])
    assert ("PERSON", "zhangsan") in found["LICENSE-zhangsan.txt"]
    assert ("PROFESSION", "COO") in found["COO_salary.csv"]


def test_public_scan_keeps_license_name_but_masks_identity(tmp_path):
    from privacyfs.core import scan as scan_public
    from privacyfs.emit.listing import to_json
    from privacyfs.pseudonym import Pseudonymizer
    root = tmp_path / "source"
    root.mkdir()
    for name in ["LICENSE-MIT", "LICENSE-APACHE", "LICENSE-zhangsan.txt", "cookie.json"]:
        (root / name).touch()
    pseudo = Pseudonymizer(tmp_path / "mapping.db")
    try:
        result = to_json(scan_public(root, Rules(use_ner=False), pseudo))
        assert "LICENSE-MIT" in result and "LICENSE-APACHE" in result and "cookie.json" in result
        assert "zhangsan" not in result and "PERSON_" in result
    finally:
        pseudo.close()
