"""Metadata scrubbing tests."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from privacyfs.config import Rules
from privacyfs.core import iter_mapped
from privacyfs.emit.mirror import MARKER, build_mirror
from privacyfs.emit.scrub import EPOCH, scrub_copy
from privacyfs.pseudonym import Pseudonymizer


@pytest.fixture
def pseudo(tmp_path: Path):
    p = Pseudonymizer(tmp_path / "mapping.db")
    yield p
    p.close()


def make_docx(path: Path) -> None:
    core = """<?xml version="1.0"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:creator>张三</dc:creator><cp:lastModifiedBy>李四</cp:lastModifiedBy>
  <dc:title>秘密计划</dc:title>
</cp:coreProperties>"""
    app = """<?xml version="1.0"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Company>某某公司</Company><Manager>王五</Manager></Properties>"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<?xml version=\"1.0\"?><Types/>")
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("word/document.xml", "<document>正文内容保留</document>")


def test_scrub_office(tmp_path: Path):
    src = tmp_path / "a.docx"
    dst = tmp_path / "b.docx"
    make_docx(src)
    assert scrub_copy(src, dst) is True
    with zipfile.ZipFile(dst) as z:
        core = z.read("docProps/core.xml").decode()
        app = z.read("docProps/app.xml").decode()
        body = z.read("word/document.xml").decode()
    assert "张三" not in core and "李四" not in core and "秘密计划" not in core
    assert "某某公司" not in app and "王五" not in app
    assert "正文内容保留" in body  # real content untouched


def test_scrub_pdf(tmp_path: Path):
    from pypdf import PdfReader, PdfWriter

    src = tmp_path / "a.pdf"
    dst = tmp_path / "b.pdf"
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.add_metadata({"/Author": "张三", "/Title": "离婚协议"})
    with open(src, "wb") as f:
        w.write(f)
    assert scrub_copy(src, dst) is True
    meta = PdfReader(dst).metadata or {}
    leaked = "".join(str(v) for v in meta.values())
    assert "张三" not in leaked and "离婚协议" not in leaked


def test_scrub_jpeg(tmp_path: Path):
    Image = pytest.importorskip("PIL.Image")
    import piexif

    src = tmp_path / "a.jpg"
    dst = tmp_path / "b.jpg"
    Image.new("RGB", (8, 8), (255, 255, 255)).save(src, "jpeg")
    exif = {"0th": {piexif.ImageIFD.Artist: "张三".encode()},
            "GPS": {piexif.GPSIFD.GPSLatitude: [(31, 1), (12, 1), (0, 1)]}}
    piexif.insert(piexif.dump(exif), str(src))
    assert scrub_copy(src, dst) is True
    loaded = piexif.load(str(dst))
    assert not loaded["0th"].get(piexif.ImageIFD.Artist)
    assert not loaded["GPS"]


def test_mirror_scrub_full(tmp_path: Path, pseudo):
    src = tmp_path / "disk"
    src.mkdir()
    make_docx(src / "张三-报告.docx")
    (src / "plain.txt").write_text("hello")
    dst = tmp_path / "view"

    mapped = iter_mapped(src, Rules(), pseudo)
    stats = build_mirror(src, mapped, dst, scrub_metadata=True)
    assert stats.scrubbed >= 1

    mirrored = next(dst.rglob("*.docx"))
    with zipfile.ZipFile(mirrored) as z:
        core = z.read("docProps/core.xml").decode()
    assert "张三" not in core
    assert not os.path.samefile(src / "张三-报告.docx", mirrored)  # real copy
    assert os.path.getmtime(mirrored) == EPOCH
    assert "张三" not in str(mirrored)  # name still pseudonymized
