"""Real DOCX/PDF byte parsing and source projection contracts."""
import pytest

from privacyfs.documents import DocumentSession, Limits, ParseStatus
from privacyfs.sample_documents import docx_bytes, pdf_bytes
from privacyfs.relations import RecordTemplate, FieldSpec, extract_records
from privacyfs.documents.models import Budget, ProcessingStopped


def parse(tmp_path, name, payload, limits=None):
    (tmp_path / name).write_bytes(payload)
    session = DocumentSession(tmp_path, limits=limits)
    return session, session.parse(session.capture(name))


def test_docx_table_and_paragraph_positions_and_unprocessed_header(tmp_path):
    data = docx_bytes(paragraphs=("cid=C001;status=secret",), tables=([['cid', 'name'], ['C001', '合成企业甲']],))
    session, doc = parse(tmp_path, "sample.docx", data)
    with session:
        assert doc.coverage.status == ParseStatus.PARTIAL, doc.coverage.reason
        assert doc.coverage.projection_complete
        assert "headers" in doc.coverage.unprocessed
        assert "SYNTHETIC_PRIVATE_HEADER" not in doc.text
        cell = next(b for b in doc.blocks if b.text == "合成企业甲")
        loc = doc.locate(cell, 0, len(cell.text))
        assert (loc.table, loc.row, loc.column, loc.part, loc.unit) == (0, 1, 1, 'word/document.xml', 'extracted_unicode_codepoint')
        assert doc.resolve(loc) == "合成企业甲"
        assert (tmp_path / "sample.docx").read_bytes() == data


def test_pdf_unicode_page_locations_metadata_and_mixed_blank_page(tmp_path):
    session, doc = parse(tmp_path, "sample.pdf", pdf_bytes(["cid=C001;name=合成企业甲", "", "mid=M1;status=secret"], attachment=True))
    with session:
        assert doc.coverage.status == ParseStatus.PARTIAL, doc.coverage.reason
        assert doc.coverage.projection_complete
        assert doc.coverage.total_parts == 3
        assert "empty_or_image_only_pages" in doc.coverage.unprocessed
        assert "pdf_names_or_attachments" in doc.coverage.unprocessed
        block = doc.blocks[0]
        at = block.text.index("合成企业甲")
        loc = doc.locate(block, at, at + 5)
        assert loc.page == 1 and loc.unit == "extracted_unicode_codepoint"
        assert doc.resolve(loc) == "合成企业甲"


@pytest.mark.parametrize("payload,reason", [(pdf_bytes(["secret"], encrypted=True), "PDF_ENCRYPTED"), (pdf_bytes([""]), "PDF_NO_TEXT"), (b"invalid PDF", "CONTAINER_PARSE_FAILED")], ids=["encrypted", "no-text", "invalid"])
def test_pdf_nonprocessable_inputs_are_never_complete(tmp_path, payload, reason):
    session, doc = parse(tmp_path, "bad.pdf", payload)
    with session:
        assert doc.coverage.status in {ParseStatus.FAILED, ParseStatus.UNSUPPORTED}
        assert doc.coverage.reason == reason


def test_pdf_page_limit_is_explicit(tmp_path):
    session, doc = parse(tmp_path, "many.pdf", pdf_bytes(["x", "y"]), Limits(max_pdf_pages=1))
    with session:
        assert doc.coverage.reason == "PDF_PAGE_LIMIT"


def test_complex_inputs_require_explicit_projection_and_nonempty_selected_page(tmp_path):
    from dataclasses import replace
    template = RecordTemplate((FieldSpec("id", "key", "ids"), FieldSpec("status", "sensitive", values=("ready",))), "id")
    session, doc = parse(tmp_path, "a.pdf", pdf_bytes(["id=C001;status=ready", ""]))
    with session:
        with pytest.raises(ProcessingStopped, match="EXPLICIT_PROJECTION_REQUIRED"):
            list(extract_records(doc, template, Budget(Limits())))
        selected = replace(template, source_view="pdf_pages", pages=(1,))
        rows = list(extract_records(doc, selected, Budget(Limits())))
        assert rows[0][0]["status"].value == "ready"
        with pytest.raises(ProcessingStopped, match="NO_TEXT"):
            list(extract_records(doc, replace(selected, pages=(2,)), Budget(Limits())))


def test_docx_table_projection_keeps_cells_separate(tmp_path):
    template = RecordTemplate((FieldSpec("id", "key", "ids"), FieldSpec("name", "identity")), "id",
                              source_view="docx_table", table_index=0)
    session, doc = parse(tmp_path, "a.docx", docx_bytes(tables=([['id', 'name'], ['0001', '张三'], ['0002', '李四']],)))
    with session:
        rows = list(extract_records(doc, template, Budget(Limits())))
        assert [r[0]["id"].value for r in rows] == ['0001', '0002']
        assert rows[1][0]["name"].locator.row == 2


@pytest.mark.parametrize("extra,flag", [
    ({'word/styles.xml':'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style><w:rPr><w:vanish/></w:rPr></w:style></w:styles>'}, "hidden_style_text"),
    ({'word/document.xml':'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:ins><w:r><w:t>id=C1;status=secret</w:t></w:r></w:ins></w:p></w:body></w:document>'}, "revisions"),
])
def test_unsupported_docx_text_semantics_cannot_be_published(tmp_path, extra, flag):
    session, doc = parse(tmp_path, "a.docx", docx_bytes(paragraphs=("id=C1;status=secret",), extra=extra))
    with session:
        assert flag in doc.coverage.unprocessed
        assert not doc.coverage.projection_complete


def test_docx_entities_duplicate_names_and_expansion_refused(tmp_path):
    import io, zipfile
    hostile = '<!DOCTYPE document [<!ENTITY x SYSTEM "file:///private.txt">]><document>&x;</document>'
    session, doc = parse(tmp_path, "entity.docx", docx_bytes(extra={'word/document.xml':hostile}))
    with session:
        assert doc.coverage.status == ParseStatus.FAILED
    session, doc = parse(tmp_path, "large.docx", docx_bytes(paragraphs=("x" * 100_000,)), Limits(max_container_bytes=4096))
    with session:
        assert doc.coverage.reason == "DOCX_EXPANSION_LIMIT"


def test_unsupported_symbol_cannot_silently_change_an_identifier(tmp_path):
    part = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>id=C</w:t><w:sym w:font="Wingdings" w:char="F031"/><w:t>001</w:t></w:r></w:p></w:body></w:document>'
    session, doc = parse(tmp_path, "symbol.docx", docx_bytes(extra={"word/document.xml": part}))
    with session:
        assert not doc.coverage.projection_complete
        assert "symbols" in doc.coverage.unprocessed


def test_docx_hidden_styles_with_nonstandard_part_name_are_detected(tmp_path):
    import io, zipfile
    original = docx_bytes(paragraphs=("id=C1;status=ready",))
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        types = archive.read('[Content_Types].xml').decode().replace('</Types>', '<Override PartName="/word/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
    styles = '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style><w:rPr><w:vanish/></w:rPr></w:style></w:styles>'
    session, doc = parse(tmp_path, "styled.docx", docx_bytes(paragraphs=("id=C1;status=ready",), extra={'[Content_Types].xml':types, 'word/custom.xml':styles}))
    with session:
        assert not doc.coverage.projection_complete
        assert 'hidden_style_text' in doc.coverage.unprocessed


@pytest.mark.skipif(__import__('os').name != 'nt', reason="Windows job memory limit")
def test_real_child_memory_limit_is_enforced():
    import subprocess, sys
    from privacyfs.documents.containers import WindowsJob
    child = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-S', '-c',
        "import sys;sys.stdin.buffer.read(1);value=bytearray(256*1024*1024);print('allocated')"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    guard = WindowsJob(child, 64 * 1024 * 1024)
    try:
        out, err = child.communicate(b'x', timeout=10)
        assert child.returncode != 0 and b'allocated' not in out
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
        guard.close()
