"""End-to-end scan tests. Core assertion: no sensitive surface ever leaks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from privacyfs.config import Rules, load_rules
from privacyfs.core import scan
from privacyfs.emit.listing import to_json
from privacyfs.pseudonym import Pseudonymizer

SENSITIVE = ["张三", "李四", "某某公司", "R18", "NSFW", "成人影片", "13812345678"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "disk"
    (root / "文档" / "张三-论文-最终稿").mkdir(parents=True)
    (root / "文档" / "李四-职称评比.xlsx").write_text("x")
    (root / "某某公司法律文书.pdf").write_text("x")
    (root / "R18").mkdir()
    (root / "R18" / "[成人影片] 收藏.mp4").write_text("x")
    (root / "照片").mkdir()
    (root / "照片" / "2024-06-01.jpg").write_text("x")
    (root / "合同-13812345678.txt").write_text("x")
    return root


@pytest.fixture
def pseudo(tmp_path: Path):
    p = Pseudonymizer(tmp_path / "mapping.db")
    yield p
    p.close()


def rules_with_names() -> Rules:
    r = Rules()
    r.literal_names = ["张三", "李四", "某某公司"]
    return r


def run(root: Path, pseudo: Pseudonymizer, rules: Rules | None = None,
        max_level: int | None = None):
    # default: built-in rules only, to prove they cover names/orgs/professions
    result = scan(root, rules or Rules(), pseudo, max_level=max_level)
    return result, to_json(result)


def assert_no_leak(text: str):
    for word in SENSITIVE:
        assert word not in text, f"leaked: {word}"


def test_no_sensitive_surface_in_output(tree, pseudo):
    _, out = run(tree, pseudo)
    assert_no_leak(out)


def test_pseudonyms_present(tree, pseudo):
    _, out = run(tree, pseudo)
    data = json.loads(out)
    paths = [e["path"] for e in data["entries"]]
    assert any("PERSON_" in p for p in paths), paths
    assert any("[HIDDEN]" in p for p in paths), paths


def test_hidden_subtree_not_listed(tree, pseudo):
    result, out = run(tree, pseudo)
    assert "[HIDDEN]" in out
    # the file inside R18/ must not appear at all
    assert "收藏" not in out
    assert result.hidden_dirs == 1


def test_phone_number_masked(tree, pseudo):
    _, out = run(tree, pseudo)
    assert "13812345678" not in out
    assert "PRIVATE_" in out


@pytest.mark.parametrize(
    "filename, secret",
    [
        ("联系13812345678.txt", "13812345678"),
        ("phone13812345678backup.txt", "13812345678"),
        ("编号110101199003078823.pdf", "110101199003078823"),
        ("ID110101199003078823copy.pdf", "110101199003078823"),
    ],
)
def test_numeric_identifiers_masked_next_to_word_characters(
    tmp_path, pseudo, filename, secret
):
    (tmp_path / filename).write_text("x")
    _, out = run(tmp_path, pseudo)
    assert secret not in out
    assert "PRIVATE_" in out


def test_mapping_deterministic(tree, pseudo):
    run(tree, pseudo)
    first = pseudo.all_mappings()
    run(tree, pseudo)
    assert first == pseudo.all_mappings()


def test_mapping_handles_stale_concurrent_allocator(tmp_path):
    db = tmp_path / "nested" / "mapping.db"
    first = Pseudonymizer(db)
    second = Pseudonymizer(db)
    try:
        assert first.alias("PERSON", "Alice") == "PERSON_0001"
        # Simulate a second process whose local counter predates the insert.
        second._counters["PERSON"] = 0
        assert second.alias("PERSON", "Bob") == "PERSON_0002"
        assert second.alias("PERSON", "Alice") == "PERSON_0001"
        assert len(first.all_mappings()) == 2
    finally:
        first.close()
        second.close()


def test_custom_rules_extend(tree, pseudo, tmp_path):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text("literal_names:\n  - 王五\nkeywords:\n  - 机密\n", encoding="utf-8")
    (tree / "王五-机密计划.docx").write_text("x")
    rules = load_rules(str(rules_file))
    _, out = run(tree, pseudo, rules)
    assert "王五" not in out
    assert "机密" not in out


@pytest.mark.parametrize(
    "yaml_text, message",
    [
        ("use_ner: 'false'\n", "must be a boolean"),
        ("use_llm: 1\n", "must be a boolean"),
        ("llm_max: nope\n", "non-negative integer"),
        ("llm_max: -1\n", "non-negative integer"),
        ("regexes:\n  - '('\n", "invalid regex"),
        ("llm_model: ''\n", "non-empty string"),
    ],
)
def test_invalid_rule_values_are_rejected(tmp_path, yaml_text, message):
    rules_file = tmp_path / "bad-rules.yaml"
    rules_file.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_rules(str(rules_file))


def test_normal_names_untouched(tree, pseudo):
    _, out = run(tree, pseudo)
    assert "2024-06-01.jpg" in out
    assert "照片" in out


def test_ner_surname_filter():
    from privacyfs.detectors.names import NameNER

    ner = NameNER()
    # "法律文书" must not be mistaken for a person
    assert ner.find("法律文书") == []
    # a real name is still caught
    found = ner.find("张伟-笔记")
    assert any(f.surface == "张伟" for f in found)


def test_org_detected_with_defaults(tree, pseudo):
    _, out = run(tree, pseudo)
    assert "某某公司" not in out
    assert "ORG_" in out


def test_english_org_detected(tmp_path, pseudo):
    (tmp_path / "Acme Inc contract.pdf").write_text("x")
    _, out = run(tmp_path, pseudo)
    assert "Acme Inc" not in out
    assert "ORG_" in out


def test_profession_masked(tmp_path, pseudo):
    (tmp_path / "软件工程师-招聘需求.docx").write_text("x")
    _, out = run(tmp_path, pseudo)
    assert "工程师" not in out
    assert "ROLE_" in out
    # 招聘需求 is not sensitive and stays readable
    assert "招聘需求" in out


def test_surname_heuristic_works_without_ner(tree, pseudo):
    rules = Rules(use_ner=False)
    _, out = run(tree, pseudo, rules)
    assert "张三" not in out
    assert "PERSON_" in out


def test_surname_heuristic_false_positives():
    from privacyfs.detectors.names import SurnameHeuristic

    h = SurnameHeuristic()
    for word in ("方法-总结", "任务管理器", "高清电影", "王者荣耀攻略"):
        assert h.find(word) == [], word
    assert any(f.surface == "张三" for f in h.find("张三-论文"))


# ---------- mirror ----------

from privacyfs.core import iter_mapped
from privacyfs.emit.mirror import MARKER, build_mirror


def mirror_paths(dst: Path) -> list[str]:
    return [
        p.relative_to(dst).as_posix()
        for p in dst.rglob("*")
        if p.name != MARKER
    ]


def test_mirror_no_real_names(tree, pseudo, tmp_path):
    dst = tmp_path / "view"
    mapped = iter_mapped(tree, Rules(), pseudo)
    build_mirror(tree, mapped, dst)
    for rel in mirror_paths(dst):
        assert_no_leak(rel)
    # placeholder for the hidden subtree exists and is empty
    assert (dst / "[HIDDEN]").is_dir()
    assert list((dst / "[HIDDEN]").iterdir()) == []


def test_mirror_hardlink_same_content(tree, pseudo, tmp_path):
    dst = tmp_path / "view"
    mapped = iter_mapped(tree, Rules(), pseudo)
    build_mirror(tree, mapped, dst)
    mirrored = dst / "照片" / "2024-06-01.jpg"
    assert mirrored.read_text() == (tree / "照片" / "2024-06-01.jpg").read_text()
    assert mirrored.samefile(tree / "照片" / "2024-06-01.jpg")


def test_mirror_rejects_dest_inside_source(tree, pseudo):
    mapped = iter_mapped(tree, Rules(), pseudo)
    with pytest.raises(ValueError):
        build_mirror(tree, mapped, tree / "view")


def test_mirror_rejects_source_inside_dest(tmp_path, pseudo):
    dst = tmp_path / "view"
    src = dst / "source"
    src.mkdir(parents=True)
    original = src / "keep.txt"
    original.write_text("must survive")
    (dst / MARKER).write_text("{}")
    mapped = iter_mapped(src, Rules(), pseudo)

    with pytest.raises(ValueError, match="source must not be inside"):
        build_mirror(src, mapped, dst, overwrite=True)

    assert original.read_text() == "must survive"


def test_mirror_refuses_foreign_dir(tree, pseudo, tmp_path):
    dst = tmp_path / "view"
    dst.mkdir()
    (dst / "user-file.txt").write_text("do not delete")
    mapped = iter_mapped(tree, Rules(), pseudo)
    with pytest.raises(ValueError):
        build_mirror(tree, mapped, dst, overwrite=True)
    assert (dst / "user-file.txt").exists()  # untouched


def test_mirror_force_rebuilds_own_tree(tree, pseudo, tmp_path):
    dst = tmp_path / "view"
    mapped = iter_mapped(tree, Rules(), pseudo)
    build_mirror(tree, mapped, dst)
    (dst / "stale.txt").write_text("old")
    build_mirror(tree, mapped, dst, overwrite=True)
    assert not (dst / "stale.txt").exists()
    assert (dst / MARKER).exists()


def test_root_name_itself_sanitized(tmp_path, pseudo):
    root = tmp_path / "张伟的硬盘"
    root.mkdir()
    (root / "a.txt").write_text("x")
    _, out = run(root, pseudo)
    assert "张伟" not in out


# ---------- sizes ----------

def test_dir_sizes_aggregated(tree, pseudo):
    result, out = run(tree, pseudo)
    data = json.loads(out)
    sizes = {e["path"]: e["size"] for e in data["entries"]}
    # every fixture file is 1 byte; 文档 holds exactly one file
    assert sizes["文档"] == 1
    assert sizes["照片"] == 1
    # hidden subtree size is summed without listing its contents
    assert sizes["[HIDDEN]"] == 1


def test_dir_size_covers_unlisted_depth(deep_tree, pseudo):
    (deep_tree / "一级" / "二级" / "深.txt").write_bytes(b"z" * 100)
    _, out = run(deep_tree, pseudo, Rules(), max_level=1)
    data = json.loads(out)
    sizes = {e["path"]: e["size"] for e in data["entries"]}
    # level-2/3 entries are not listed, but their bytes land in 一级
    assert "二级" not in sizes
    assert sizes["一级"] == 101  # 中.txt (1B) + 深.txt (100B)


def test_render_tree_right_aligned(tree, pseudo):
    from rich.cells import cell_len

    from privacyfs.emit.listing import render_tree

    result, _ = run(tree, pseudo)
    text = render_tree(result, show_size=True)
    sized = [ln for ln in text.splitlines() if ln.rstrip().endswith(" B")]
    assert sized, text
    ends = {cell_len(ln) for ln in sized}
    assert len(ends) == 1  # every size column ends at the same display column


def test_render_tree_no_size(tree, pseudo):
    from privacyfs.emit.listing import render_tree

    result, _ = run(tree, pseudo)
    text = render_tree(result, show_size=False)
    assert " B" not in text
    assert "文档" in text


def test_render_tree_fits_terminal_width(tree, pseudo):
    from rich.cells import cell_len

    from privacyfs.emit.listing import render_tree

    long_name = "很长的目录名" * 10
    (tree / long_name).mkdir()
    (tree / long_name / "f.txt").write_text("x")
    result, _ = run(tree, pseudo)
    text = render_tree(result, show_size=True, width=40)
    for ln in text.splitlines():
        assert cell_len(ln) <= 40, ln
    assert "…" in text  # the long name was truncated, not wrapped


def test_tui_app_fills_rows():
    pytest.importorskip("textual")
    import asyncio

    from privacyfs.core import ScanResult, SanitizedEntry
    from privacyfs.emit.tui import create_app

    holder = {
        "result": ScanResult("root", [SanitizedEntry("a.txt", False, 5)], 1, 0, 0),
        "msg": "",
    }
    app = create_app(holder)

    async def drive():
        async with app.run_test() as pilot:
            await pilot.pause(0.4)
            from textual.widgets import DataTable

            assert app.query_one(DataTable).row_count == 2  # root + file

    asyncio.run(drive())


def test_parallel_worker_matches_serial(tmp_path):
    """The process-pool worker must produce exactly what the in-process
    plain detectors produce."""
    from privacyfs.core import _detectors_for
    from privacyfs.parallel import detect_chunk, init_worker

    rules = Rules()
    rules.use_ner = False  # speed: jieba init per call
    init_worker(rules)
    parts = ["张三-论文.docx", "notes.txt", "某某公司合同.pdf", "zhangsan.txt"]
    got = detect_chunk(parts)

    dets = [d for d in _detectors_for(rules, None)
            if not hasattr(d, "find_batch") and not getattr(d, "late", False)]
    want: dict[str, list] = {}
    for p in parts:
        fs = [(f.category, f.surface) for d in dets for f in d.find(p)]
        if fs:
            want[p] = fs
    assert got == want


def test_walk_parallel_matches_serial(tree):
    from privacyfs.scanner import walk, walk_parallel

    args = (tree, Rules().excludes, lambda n: "r18" in n.lower())
    serial = sorted(e.relpath.as_posix() for e in walk(*args, with_size=False))
    parallel = sorted(e.relpath.as_posix()
                      for e in walk_parallel(*args, with_size=False, workers=4))
    assert serial == parallel


def test_cancel_stops_scan(tree, pseudo):
    import threading

    from privacyfs.core import ScanCancelled
    from privacyfs.scanner import walk, walk_parallel

    cancel = threading.Event()
    cancel.set()
    # walk stops immediately
    assert list(walk(tree, [], lambda n: False, cancel=cancel)) == []
    assert list(walk_parallel(tree, [], lambda n: False, cancel=cancel)) == []
    # and iter_mapped raises instead of producing a listing
    with pytest.raises(ScanCancelled):
        iter_mapped(tree, Rules(), pseudo, cancel=cancel)


# ---------- unicode normalization ----------

def test_fullwidth_and_traditional(tmp_path, pseudo):
    (tmp_path / "Ｒ１８").mkdir()                      # full-width R18
    (tmp_path / "Ｒ１８" / "x.mp4").write_text("x")
    (tmp_path / "裏番合集.txt").write_text("x")        # traditional keyword
    (tmp_path / "張三-論文.txt").write_text("x")       # traditional name
    _, out = run(tmp_path, pseudo)
    assert "Ｒ１８" not in out
    assert "x.mp4" not in out           # hidden subtree not descended
    assert "裏番" not in out
    assert "張三" not in out
    assert "論文" not in out


# ---------- english names ----------

def test_english_names(tmp_path, pseudo):
    (tmp_path / "John Smith resume.docx").write_text("x")
    (tmp_path / "New Folder").mkdir()
    (tmp_path / "Hello World.txt").write_text("x")
    result, out = run(tmp_path, pseudo)
    assert "John Smith" not in out
    assert "PERSON_" in out
    assert "New Folder" in out          # not a name
    assert "Hello World.txt" in out


def test_english_names_disabled(tmp_path, pseudo):
    (tmp_path / "John Smith notes.docx").write_text("x")
    _, out = run(tmp_path, pseudo, Rules(detect_en_names=False))
    assert "John Smith notes.docx" in out


# ---------- paranoid ----------

def test_paranoid_regexes(tmp_path, pseudo):
    (tmp_path / "QQ123456789.txt").write_text("x")
    _, out_default = run(tmp_path, pseudo, Rules())
    assert "QQ123456789.txt" in out_default   # default: not masked
    _, out_paranoid = run(tmp_path, pseudo, Rules(paranoid=True))
    assert "123456789" not in out_paranoid


# ---------- output hygiene ----------

def test_no_size(tmp_path, pseudo):
    (tmp_path / "a.txt").write_text("hello")
    result, _ = run(tmp_path, pseudo)
    out = to_json(result, include_size=False)
    assert '"size"' not in out


def test_db_auto_excluded(tmp_path, pseudo):
    db_file = tmp_path / "mapping.db"
    db_file.write_text("x")
    from privacyfs.scanner import norm_path

    mapped = iter_mapped(tmp_path, Rules(), pseudo, frozenset({norm_path(db_file.resolve())}))
    assert all(e.raw.name != "mapping.db" for e in mapped)


def test_write_ignore(tmp_path, pseudo):
    from privacyfs.cli import _write_ignore_files

    (tmp_path / "张三-论文.txt").write_text("x")
    (tmp_path / "clean.txt").write_text("x")
    mapped = iter_mapped(tmp_path, Rules(), pseudo)
    n = _write_ignore_files(tmp_path, mapped)
    assert n == 1
    ignore = (tmp_path / ".ignore").read_text(encoding="utf-8")
    assert "张三-论文.txt" in ignore
    assert "clean.txt" not in ignore


# ---------- long paths ----------

import os

import pytest as _pytest


@_pytest.mark.skipif(os.name != "nt", reason="Windows long-path test")
def test_long_path_scan(tmp_path, pseudo):
    from privacyfs.scanner import winlong

    deep = tmp_path
    for i in range(12):
        deep = deep / ("深" * 20 + str(i))
    os.makedirs(winlong(deep))
    with open(winlong(deep / "张三-秘密.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    _, out = run(tmp_path, pseudo)
    assert "张三" not in out
    assert "秘密" in out or "SENSITIVE_" in out or "PERSON_" in out


def test_cli_bare_and_default_path_scan_cwd(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from privacyfs.cli import app

    (tmp_path / "张三-论文.txt").write_text("x")
    (tmp_path / "clean.txt").write_text("x")
    monkeypatch.setenv("PRIVACYFS_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    for argv in ([], ["scan"]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output
        assert "张三" not in result.output
        assert "PERSON_" in result.output
        assert "clean.txt" in result.output


# ---------- hanlp NER (optional engine) ----------

def _hanlp_installed() -> bool:
    try:
        import hanlp  # noqa: F401

        return True
    except ImportError:
        return False


requires_hanlp = pytest.mark.skipif(not _hanlp_installed(), reason="hanlp not installed")


@requires_hanlp
def test_hanlp_rare_name_and_org(tmp_path, pseudo):
    (tmp_path / "王富贵-借款合同.txt").write_text("x")
    (tmp_path / "张伟的北京大学录取通知书.pdf").write_text("x")
    _, out = run(tmp_path, pseudo, Rules(ner_engine="hanlp"))
    assert "王富贵" not in out
    assert "北京大学" not in out


@requires_hanlp
def test_hanlp_no_false_positive_on_plain_words(tmp_path, pseudo):
    (tmp_path / "照片备份").mkdir()
    (tmp_path / "照片备份" / "风景照 001.jpg").write_text("x")
    _, out = run(tmp_path, pseudo, Rules(ner_engine="hanlp"))
    assert "照片备份" in out
    assert "风景照 001.jpg" in out


# ---------- recursion depth ----------

@pytest.fixture
def deep_tree(tmp_path: Path) -> Path:
    root = tmp_path / "disk"
    (root / "一级" / "二级").mkdir(parents=True)
    (root / "一级" / "二级" / "深.txt").write_text("x")
    (root / "一级" / "中.txt").write_text("x")
    (root / "浅.txt").write_text("x")
    return root


def test_default_nonrecursive(deep_tree, pseudo):
    _, out = run(deep_tree, pseudo, Rules(), max_level=1)
    assert "浅.txt" in out
    assert "一级" in out
    assert "中.txt" not in out
    assert "深.txt" not in out


def test_recursive_two_levels(deep_tree, pseudo):
    _, out = run(deep_tree, pseudo, Rules(), max_level=2)
    assert "中.txt" in out
    assert "二级" in out
    assert "深.txt" not in out


def test_recursive_unlimited(deep_tree, pseudo):
    _, out = run(deep_tree, pseudo, Rules())
    assert "深.txt" in out


def test_massage_argv():
    from privacyfs.cli import _massage_argv

    assert _massage_argv(["scan", "-r"]) == ["scan", "-r", "0"]
    assert _massage_argv(["scan", "-r", "2"]) == ["scan", "-r", "2"]
    assert _massage_argv(["scan", "--recursive"]) == ["scan", "--recursive", "0"]
    assert _massage_argv(["scan", "-r", "-f", "json"]) == ["scan", "-r", "0", "-f", "json"]
    assert _massage_argv(["scan", "-r2"]) == ["scan", "-r2"]
    assert _massage_argv(["scan", "."]) == ["scan", "."]


# ---------- pinyin names ----------

def test_pinyin_forms():
    from privacyfs.detectors.pinyin import PinyinDetector

    det = PinyinDetector()
    hits = [
        "ZhangSan-论文.docx", "zhangsan.txt", "Zhang San 简历.pdf",
        "Zhang_San-final.xlsx", "San Zhang.vcf", "Zhang-San 2.zip",
        "WangXiaoMing 照片.jpg", "liSi_备份.rar", "LISI.rar",
        "ouyangfeng.txt",
    ]
    for name in hits:
        found = det.find(name)
        assert found, name
        assert all(f.category == "PERSON" for f in found)


def test_pinyin_false_positives():
    from privacyfs.detectors.pinyin import PinyinDetector

    det = PinyinDetector()
    safe = [
        "data.csv", "memo.txt", "lime-theme", "hello world.doc",
        "New Folder", "zhang san bao bei.txt".replace(" ", ""),  # 3 given syllables: no
        "index.html", "main.py", "readme.md",
    ]
    for name in safe:
        assert det.find(name) == [], name


def test_pinyin_allowlist():
    from privacyfs.detectors.pinyin import PinyinDetector

    assert PinyinDetector().find("zhangsan.txt") != []
    assert PinyinDetector(["zhangsan"]).find("zhangsan.txt") == []
    # default blocklist keeps "lime" (li+me) quiet
    assert PinyinDetector().find("lime-docs") == []


def test_pinyin_in_scan(tmp_path, pseudo):
    (tmp_path / "ZhangSan-论文.docx").write_text("x")
    _, out = run(tmp_path, pseudo)
    assert "ZhangSan" not in out
    assert "PERSON_" in out


# ---------- identity-implying terms ----------

def test_identity_terms_masked(tmp_path, pseudo):
    names = [
        "2024奖学金名单.xlsx",   # -> 学生身份
        "毕业论文答辩安排.docx",  # -> 学生身份
        "取保候审通知书.pdf",     # -> 涉案
        "住院记录.pdf",          # -> 医疗
        "房贷合同扫描件.pdf",     # -> 金融
        "入党申请书.docx",       # -> 政治面貌
    ]
    for n in names:
        (tmp_path / n).write_text("x")
    _, out = run(tmp_path, pseudo)
    for n in names:
        stem = n.rsplit(".", 1)[0]
        assert stem not in out, stem
    assert "IDENT_" in out


def test_identity_disabled(tmp_path, pseudo):
    (tmp_path / "2024奖学金名单.xlsx").write_text("x")
    _, out = run(tmp_path, pseudo, Rules(detect_identity=False))
    assert "2024奖学金名单.xlsx" in out


# ---------- local LLM detector ----------

def test_llm_parse_response():
    from privacyfs.detectors.llm import LLMDetector

    chunk = ["a.txt", "b.txt", "c.txt"]
    # tolerate the model's format drift
    out = LLMDetector.parse_response("1:PERSON\n2. 类别：NONE\n3、IDENTITY\n", chunk)
    assert out == {"a.txt": "PERSON", "b.txt": "NONE", "c.txt": "IDENTITY"}
    # junk / out-of-range indices are ignored
    assert LLMDetector.parse_response("blah\n9:PERSON", chunk) == {}
    # models that drop numbering: positional fallback when line count matches
    out = LLMDetector.parse_response("NONE\nPERSON\nNONE\n", chunk)
    assert out == {"a.txt": "NONE", "b.txt": "PERSON", "c.txt": "NONE"}


class _StubHandler:
    pass


def _ollama_stub():
    """A tiny HTTP stub mimicking Ollama's /api/chat."""
    import json as _json
    import re as _re
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            content = body["messages"][0]["content"]
            lines = []
            for line in content.splitlines():
                m = _re.match(r"(\d+)\.\s*(.+)", line)
                if m:
                    label = "IDENTITY" if "青禾" in m.group(2) else "NONE"
                    lines.append(f"{m.group(1)}:{label}")
            payload = _json.dumps({"message": {"content": "\n".join(lines)}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_llm_unparsed_items_are_not_cached(monkeypatch):
    from privacyfs.detectors.llm import LLMDetector

    class Cache:
        def __init__(self):
            self.saved = {}

        def llm_cache_get(self, names):
            return {name: self.saved[name] for name in names if name in self.saved}

        def llm_cache_put(self, verdicts):
            self.saved.update(verdicts)

    cache = Cache()
    detector = LLMDetector(cache=cache, batch_size=2)
    monkeypatch.setattr(detector, "_chat", lambda prompt, size: "1:PERSON")

    out = detector.find_batch(["张伟别名.txt", "未解析项目.txt"])

    assert "张伟别名.txt" in out
    assert cache.saved == {"张伟别名.txt": "PERSON"}
    assert "未解析项目.txt" not in cache.saved


def test_llm_scan_with_stub(tmp_path, pseudo):
    server = _ollama_stub()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        (tmp_path / "青禾计划二期.txt").write_text("x")   # rules miss this
        (tmp_path / "季度复盘纪要.txt").write_text("x")   # clean
        rules = Rules(use_llm=True, llm_url=url, ner_engine="off")
        rules.use_ner = False
        _, out = run(tmp_path, pseudo, rules)
        assert "青禾计划二期" not in out
        assert "IDENT_" in out
        assert "季度复盘纪要.txt" in out
        # second run: verdicts come from the persistent cache (stub still fine)
        _, out2 = run(tmp_path, pseudo, rules)
        assert "青禾计划二期" not in out2
    finally:
        server.shutdown()


def test_llm_graceful_when_down(tmp_path, pseudo):
    (tmp_path / "普通文件.txt").write_text("x")
    rules = Rules(use_llm=True, llm_url="http://127.0.0.1:9", ner_engine="off")
    rules.use_ner = False
    result, out = run(tmp_path, pseudo, rules)  # must not raise
    assert "普通文件.txt" in out


def test_cli_bare_r_via_subprocess(tmp_path):
    import subprocess
    import sys

    (tmp_path / "一级").mkdir()
    (tmp_path / "一级" / "深.txt").write_text("x")
    (tmp_path / "浅.txt").write_text("x")

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "privacyfs.cli", *args, "--db", str(tmp_path / "m.db"), "--ner", "off"],
            capture_output=True, text=True, cwd=tmp_path,
        )

    r1 = cli("scan")            # default: top level only
    assert r1.returncode == 0, r1.stderr
    assert "浅.txt" in r1.stdout and "深.txt" not in r1.stdout

    r2 = cli("scan", "-r")      # bare -r: unlimited
    assert "深.txt" in r2.stdout, r2.stderr

    r3 = cli("scan", "-r", "2")
    assert "深.txt" in r3.stdout, r3.stderr

    # bare command with flags: `privacyfs -r 2` == `privacyfs scan -r 2`
    r4 = cli("-r", "2")
    assert r4.returncode == 0, r4.stderr
    assert "深.txt" in r4.stdout
    r5 = cli("-r")
    assert "深.txt" in r5.stdout, r5.stderr
