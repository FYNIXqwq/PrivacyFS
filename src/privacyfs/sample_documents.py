"""Synthetic document fixtures for the shipped D5 trial, never customer data."""
from __future__ import annotations

import io
from xml.sax.saxutils import escape
import zipfile


def docx_bytes(*, paragraphs=(), tables=(), header="SYNTHETIC_PRIVATE_HEADER", extra=None):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    def paragraph(value):
        runs = '<w:br/>'.join('<w:t xml:space="preserve">' + escape(line) + '</w:t>' for line in value.split('\n'))
        return '<w:p><w:r>' + runs + '</w:r></w:p>'
    body = "".join(paragraph(p) for p in paragraphs)
    for table in tables:
        body += '<w:tbl><w:tblPr/><w:tblGrid>' + '<w:gridCol w:w="2500"/>' * len(table[0]) + '</w:tblGrid>'
        for row in table:
            body += '<w:tr>' + ''.join('<w:tc><w:tcPr/>' + paragraph(cell) + '</w:tc>' for cell in row) + '</w:tr>'
        body += '</w:tbl>'
    body += '<w:sectPr><w:headerReference w:type="default" r:id="header"/></w:sectPr>'
    parts = {
        '[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="main" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        'word/document.xml': f'<w:document xmlns:w="{w}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>{body}</w:body></w:document>',
        'word/header1.xml': f'<w:hdr xmlns:w="{w}">{paragraph(header)}</w:hdr>',
        'word/_rels/document.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="header" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>',
    }
    parts.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return output.getvalue()


def pdf_bytes(pages, *, encrypted=False, attachment=False):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, ArrayObject, NumberObject, TextStringObject
    writer = PdfWriter()
    all_chars = sorted(set("".join(pages)) - {"\n"})
    codes = {char: index + 1 for index, char in enumerate(all_chars)}
    cmap = DecodedStreamObject()
    mappings = "\n".join(f"<{code:04x}> <{char.encode('utf-16-be').hex()}>" for char, code in codes.items())
    cmap.set_data(("/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n/CMapName /Synthetic def /CMapType 2 def\n1 begincodespacerange <0000> <ffff> endcodespacerange\n" + str(len(codes)) + " beginbfchar\n" + mappings + "\nendbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end").encode())
    descendant = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/CIDFontType2'),
        NameObject('/BaseFont'): NameObject('/Synthetic'), NameObject('/CIDSystemInfo'): DictionaryObject({
            NameObject('/Registry'): TextStringObject('Adobe'),
            NameObject('/Ordering'): TextStringObject('Identity'), NameObject('/Supplement'): NumberObject(0)}), NameObject('/DW'): NumberObject(500)})
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type0'),
        NameObject('/BaseFont'): NameObject('/Synthetic'), NameObject('/Encoding'): NameObject('/Identity-H'),
        NameObject('/DescendantFonts'): ArrayObject([writer._add_object(descendant)]), NameObject('/ToUnicode'): writer._add_object(cmap)})
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=600, height=800)
        if text:
            stream = DecodedStreamObject()
            lines = ["BT /F1 12 Tf 14 TL 40 740 Td"]
            for line in text.splitlines():
                lines.append("<" + "".join(f"{codes[c]:04x}" for c in line) + "> Tj T*")
            lines.append("ET")
            stream.set_data("\n".join(lines).encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
    writer.add_metadata({"/Author": "SYNTHETIC_PRIVATE_AUTHOR"})
    if attachment:
        writer.add_attachment("private.txt", b"SYNTHETIC_PRIVATE_ATTACHMENT")
    if encrypted:
        writer.encrypt("synthetic-test-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def trial_pdf_bytes():
    """Viewer-friendly ASCII trial PDF using the standard Helvetica font."""
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page = writer.add_blank_page(width=600, height=800)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 16 TL 40 740 Td (mid=M001;status=paused) Tj T* (mid=M002;status=ended) Tj ET")
    page[NameObject('/Contents')] = writer._add_object(stream)
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    writer.add_metadata({"/Author": "SYNTHETIC_PRIVATE_AUTHOR"})
    writer.add_attachment("private.txt", b"SYNTHETIC_PRIVATE_ATTACHMENT")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def create_trial(destination):
    from pathlib import Path
    import json
    from .release import checked_directory, write_new
    destination = Path(destination).absolute()
    checked_directory(destination.parent)
    destination.mkdir()  # never merge into or overwrite an existing directory
    source = destination / "source"
    source.mkdir()
    write_new(source / "clients.docx", docx_bytes(paragraphs=("合成试用材料；仅选取下方表格。",), tables=([['cid', 'name'], ['C001', '合成示例企业甲']],)))
    write_new(source / "meetings.csv", b"cid,mid\nC001,M001\nC001,M002\n")
    write_new(source / "notes.pdf", trial_pdf_bytes())
    cid = {"name": "cid", "role": "key", "namespace": "customers", "entity_type": "client"}
    mid = {"name": "mid", "role": "key", "namespace": "meetings", "entity_type": "event"}
    settings = {"schema_version": "d2-templates-1", "files": [
        {"path": "clients.docx", "primary": "cid", "source_view": "docx_table", "table_index": 0,
         "fields": [cid, {"name": "name", "role": "identity"}]},
        {"path": "meetings.csv", "primary": "cid", "fields": [cid, mid]},
        {"path": "notes.pdf", "primary": "mid", "source_view": "pdf_pages", "pages": [1],
         "fields": [mid, {"name": "status", "role": "sensitive", "values": ["paused", "ended"]}]}]}
    profile = {"schema_version": "d3-task-profile-1", "task": "collaboration_statistics", "recipient": "synthetic-local-reviewer",
        "fields": [{"name": "cid", "output_name": "customer_id", "mode": "alias"},
                   {"name": "mid", "output_name": "meeting_id", "mode": "alias"},
                   {"name": "status", "output_name": "status", "approved_values": ["paused", "ended"]}]}
    write_new(source / "relations.json", json.dumps(settings, ensure_ascii=False, indent=2).encode())
    write_new(source / "profile.json", json.dumps(profile, ensure_ascii=False, indent=2).encode())
    write_new(destination / "README.txt", ("Synthetic local trial: DOCX selected table + CSV index + PDF page 1 -> new CSV files.\n"
        "Headers, PDF metadata and attached private.txt must never appear in output.\n"
        "Expected status counts: paused=1, ended=1.\n"
        "Run workspace init against source with source/relations.json and source/profile.json.\n"
        "Keep private state and output outside source. Follow the trial guide to review, approve and export.\n").encode())
    return {"schema_version": "d5-trial-1", "source_files": 3, "expected_status_counts": {"paused": 1, "ended": 1}, "synthetic": True}
