"""D0 safety regressions. All mutation targets are synthetic tmp_path trees."""
import json
import os
from pathlib import Path
import zipfile

import pytest
from typer.testing import CliRunner

from privacyfs.cli import app
from privacyfs.config import Rules
from privacyfs.core import iter_mapped
from privacyfs.emit.mirror import MARKER, build_mirror
from privacyfs.emit.scrub import EPOCH, scrub_copy
from privacyfs.models import MappedEntry
from privacyfs.pseudonym import Pseudonymizer


def simple_rules(**kwargs):
    return Rules(use_ner=False, detect_pinyin=False, detect_en_names=False, **kwargs)


def mapped_tree(root, tmp_path, **kwargs):
    pseudo = Pseudonymizer(tmp_path / "mapping.db")
    try:
        return iter_mapped(root, simple_rules(**kwargs), pseudo, with_size=False)
    finally:
        pseudo.close()


def test_marker_is_reserved_without_modifying_source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / MARKER).write_bytes(b"original user content")
    mapped = mapped_tree(root, tmp_path)
    assert mapped[0].clean.casefold() != MARKER.casefold()
    dst = tmp_path / "view"
    build_mirror(root, mapped, dst)
    assert (root / MARKER).read_bytes() == b"original user content"
    assert (dst / mapped[0].clean).read_bytes() == b"original user content"
    assert json.loads((dst / MARKER).read_text())["created_by"] == "privacyfs"


def test_directory_alias_collision_keeps_ownership_and_listing(tmp_path):
    root = tmp_path / "source"
    for dirname, filename, body in [("张三", "a.txt", "A"), ("PERSON_0001", "b.txt", "B")]:
        (root / dirname).mkdir(parents=True)
        (root / dirname / filename).write_text(body)
    mapped = mapped_tree(root, tmp_path, literal_names=["张三"])
    paths = [e.clean.casefold() for e in mapped]
    assert len(paths) == len(set(paths))
    dirs = {e.raw.as_posix(): e.clean for e in mapped if e.is_dir}
    assert dirs["张三"] != dirs["PERSON_0001"]
    for entry in mapped:
        if not entry.is_dir:
            assert entry.clean.startswith(dirs[entry.raw.parts[0]] + "/")
    dst = tmp_path / "view"
    stats = build_mirror(root, mapped, dst, force_copy=True)
    assert stats.collisions >= 1
    for entry in mapped:
        if not entry.is_dir:
            assert (dst / entry.clean).read_bytes() == (root / entry.raw).read_bytes()


def test_file_directory_alias_collision_is_resolved_before_emission(tmp_path):
    root = tmp_path / "source"
    (root / "张三").mkdir(parents=True)
    (root / "张三" / "a.txt").write_text("child")
    (root / "PERSON_0001").write_text("literal file")
    mapped = mapped_tree(root, tmp_path, literal_names=["张三"])
    assert len({e.clean.casefold() for e in mapped}) == len(mapped)
    dst = tmp_path / "view"
    build_mirror(root, mapped, dst, force_copy=True)
    for e in mapped:
        assert (dst / e.clean).is_dir() == e.is_dir


@pytest.mark.parametrize("clean", ["../escape.txt", "/escape.txt", "C:escape.txt", "a/../escape.txt", MARKER])
def test_invalid_mirror_targets_rejected_before_touching_old_view(tmp_path, clean, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_text("A")
    dst = tmp_path / "view"
    build_mirror(root, [MappedEntry(Path("a.txt"), "a.txt", False)], dst, force_copy=True)
    old = (dst / MARKER).read_bytes()
    # Guard the test itself: a regressed validator must not write the absolute
    # path used as adversarial input, even when tests run with elevated rights.
    def guard(original):
        def checked(source, target, *args, **kwargs):
            if not Path(target).resolve().is_relative_to(tmp_path.resolve()):
                raise RuntimeError("test prevented an out-of-fixture write")
            return original(source, target, *args, **kwargs)
        return checked
    import privacyfs.emit.mirror as mirror
    monkeypatch.setattr(mirror.os, "link", guard(mirror.os.link))
    monkeypatch.setattr(mirror.shutil, "copy2", guard(mirror.shutil.copy2))
    with pytest.raises(ValueError):
        build_mirror(root, [MappedEntry(Path("a.txt"), clean, False)], dst, overwrite=True)
    assert (dst / "a.txt").read_text() == "A"
    assert (dst / MARKER).read_bytes() == old


def test_out_of_tree_source_rejected(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (tmp_path / "private.txt").write_text("private")
    with pytest.raises(ValueError):
        build_mirror(root, [MappedEntry(Path("../private.txt"), "safe.txt", False)], tmp_path / "view")
    assert not (tmp_path / "view").exists()


def test_copy_failure_preserves_previous_mirror(tmp_path, monkeypatch):
    import privacyfs.emit.mirror as mirror
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_text("A")
    mapped = mapped_tree(root, tmp_path)
    dst = tmp_path / "view"
    build_mirror(root, mapped, dst, force_copy=True)
    old_marker = (dst / MARKER).read_bytes()
    (root / "a.txt").write_text("NEW")
    def fail(*args, **kwargs):
        raise OSError("synthetic private path must not reach public output")
    monkeypatch.setattr(mirror.shutil, "copy2", fail)
    with pytest.raises(OSError):
        build_mirror(root, mapped, dst, overwrite=True, force_copy=True)
    assert (dst / "a.txt").read_text() == "A"
    assert (dst / MARKER).read_bytes() == old_marker
    assert (root / "a.txt").read_text() == "NEW"


def test_directory_switch_failure_restores_old_view(tmp_path, monkeypatch):
    import privacyfs.emit.mirror as mirror
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_text("A")
    mapped = mapped_tree(root, tmp_path)
    dst = tmp_path / "view"
    build_mirror(root, mapped, dst, force_copy=True)
    old_marker = (dst / MARKER).read_bytes()
    (root / "a.txt").write_text("NEW")
    original = mirror.os.replace
    def fail_commit(src, target):
        if Path(target) == dst and ".privacyfs-build-" in Path(src).name:
            raise PermissionError("synthetic occupied destination")
        return original(src, target)
    monkeypatch.setattr(mirror.os, "replace", fail_commit)
    with pytest.raises(OSError):
        build_mirror(root, mapped, dst, overwrite=True, force_copy=True)
    assert (dst / "a.txt").read_text() == "A"
    assert (dst / MARKER).read_bytes() == old_marker


def test_copy_failure_cli_is_nonzero_and_does_not_echo_exception(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.txt").write_text("A")
    def fail(*args, **kwargs):
        raise OSError("SYNTHETIC_SECRET")
    monkeypatch.setattr("privacyfs.emit.mirror.shutil.copy2", fail)
    result = CliRunner().invoke(app, ["mirror", str(root), str(tmp_path / "view"),
        "--copy", "--ner", "off", "--db", str(tmp_path / "cli.db")])
    assert result.exit_code != 0
    assert "mirror ready" not in result.output
    assert "SYNTHETIC_SECRET" not in result.output
    assert not (tmp_path / "view").exists()


def test_valid_xml_prefix_variants_are_scrubbed(tmp_path):
    src, dst = tmp_path / "a.docx", tmp_path / "b.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("docProps/core.xml", '<p:coreProperties xmlns:p="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:d="http://purl.org/dc/elements/1.1/"><d:creator>SECRET_AUTHOR</d:creator></p:coreProperties>')
        z.writestr("docProps/app.xml", '<a:Properties xmlns:a="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><a:Company>SECRET_COMPANY</a:Company></a:Properties>')
        z.writestr("word/document.xml", "untouched body")
    assert scrub_copy(src, dst)
    with zipfile.ZipFile(dst) as z:
        assert b"SECRET_AUTHOR" not in z.read("docProps/core.xml")
        assert b"SECRET_COMPANY" not in z.read("docProps/app.xml")
        assert z.read("word/document.xml") == b"untouched body"


def test_pdf_scrub_preserves_outline_and_clears_custom_info(tmp_path):
    from pypdf import PdfReader, PdfWriter
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_outline_item("Chapter 1", 0)
    writer.add_metadata({"/CustomSecret": "PRIVATE_METADATA"})
    with src.open("wb") as f:
        writer.write(f)
    assert scrub_copy(src, dst)
    result = PdfReader(dst)
    assert len(result.pages) == 1 and len(result.outline) == 1
    assert result.outline[0].title == "Chapter 1"
    assert "PRIVATE_METADATA" not in str(result.metadata)


def test_scrub_counts_and_final_timestamps(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "plain.txt").write_text("A")
    dst = tmp_path / "view"
    stats = build_mirror(root, mapped_tree(root, tmp_path), dst, scrub_metadata=True)
    marker = json.loads((dst / MARKER).read_text())
    assert stats.unscrubbed == 1
    assert marker["files"] == 1
    assert dst.stat().st_mtime == EPOCH
    assert (dst / MARKER).stat().st_mtime == EPOCH


def test_enforce_public_root_is_neutral(tmp_path):
    root = tmp_path / "北京-2024-06-17"
    root.mkdir()
    (root / "住院记录.txt").write_text("A")
    result = CliRunner().invoke(app, ["scan", str(root), "--context-mode", "enforce",
        "--ner", "off", "-f", "json", "--db", str(tmp_path / "root.db")])
    assert result.exit_code == 0, result.output
    assert "北京" not in result.stdout and "2024-06-17" not in result.stdout


def test_foreign_marker_is_not_sufficient_for_force(tmp_path):
    root, dst = tmp_path / "source", tmp_path / "foreign"
    root.mkdir()
    dst.mkdir()
    (dst / MARKER).write_text("{}")
    (dst / "keep.txt").write_text("user content")
    with pytest.raises(ValueError):
        build_mirror(root, [], dst, overwrite=True)
    assert (dst / "keep.txt").read_text() == "user content"


def test_partial_scrub_does_not_claim_success_for_unsafe_xml(tmp_path):
    src, dst = tmp_path / "a.docx", tmp_path / "b.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("docProps/core.xml", '<!DOCTYPE a [<!ENTITY secret "synthetic">]><a>&secret;</a>')
    assert scrub_copy(src, dst) is False
    assert src.read_bytes() == dst.read_bytes()


def test_pdf_xmp_bytes_are_removed(tmp_path):
    from pypdf import PdfReader, PdfWriter
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.xmp_metadata = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">SYNTHETIC_XMP_SECRET</x:xmpmeta>'
    with src.open("wb") as f:
        writer.write(f)
    assert scrub_copy(src, dst)
    assert PdfReader(dst).xmp_metadata is None
    assert b"SYNTHETIC_XMP_SECRET" not in dst.read_bytes()


def test_scrub_rejects_source_alias_before_writing(tmp_path):
    src, dst = tmp_path / "a.docx", tmp_path / "b.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("word/document.xml", "original content")
    original = src.read_bytes()
    os.link(src, dst)
    with pytest.raises(ValueError, match="same file"):
        scrub_copy(src, dst)
    assert src.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization")
def test_unc_long_path_normalization_round_trip():
    from privacyfs.scanner import norm_path, winlong
    raw = "\\\\server\\share\\" + "long\\" * 60 + "file.txt"
    extended = winlong(raw)
    assert extended.startswith("\\\\?\\UNC\\server\\share\\")
    assert norm_path(extended) == norm_path(raw)


def test_office_preserves_namespace_prefixes_used_in_type_values(tmp_path):
    from xml.dom.minidom import parseString
    src, dst = tmp_path / "a.docx", tmp_path / "b.docx"
    xml = '''<cp:coreProperties
      xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:dcterms="http://purl.org/dc/terms/"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <dc:creator>SECRET</dc:creator>
      <dcterms:created xsi:type="dcterms:W3CDTF">2020-01-01T00:00:00Z</dcterms:created>
    </cp:coreProperties>'''
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("docProps/core.xml", xml)
    assert scrub_copy(src, dst)
    with zipfile.ZipFile(dst) as z:
        doc = parseString(z.read("docProps/core.xml"))
    assert doc.documentElement.getAttribute("xmlns:dcterms") == "http://purl.org/dc/terms/"
    created = doc.getElementsByTagNameNS("http://purl.org/dc/terms/", "created")[0]
    assert created.getAttributeNS("http://www.w3.org/2001/XMLSchema-instance", "type") == "dcterms:W3CDTF"
