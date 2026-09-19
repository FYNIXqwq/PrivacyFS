"""Bounded child-process DOCX/PDF text extraction. No external resource fetching."""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import PurePosixPath
import sys
import time
import zipfile

from defusedxml import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class Refused(Exception):
    pass


def docx(payload, limits, check):
    units, unprocessed = [], {"package_metadata", "styles_and_layout"}
    complete = True
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        raise Refused("INVALID_DOCX") from None
    with archive:
        entries = archive.infolist()
        names = [e.filename for e in entries]
        if len(entries) > limits["max_zip_entries"] or len(names) != len(set(names)):
            raise Refused("DOCX_PACKAGE_LIMIT")
        if sum(e.file_size for e in entries) > limits["max_container_bytes"]:
            raise Refused("DOCX_EXPANSION_LIMIT")
        for entry in entries:
            check()
            parts = PurePosixPath(entry.filename).parts
            if ".." in parts or entry.filename.startswith(("/", "\\")) or "\\" in entry.filename or ":" in entry.filename:
                raise Refused("INVALID_DOCX_PACKAGE_PATH")
            if entry.flag_bits & 1 or entry.file_size > limits["max_container_bytes"] or entry.file_size / max(1, entry.compress_size) > 200:
                raise Refused("DOCX_EXPANSION_LIMIT")
        def xml(name):
            data = archive.read(name)
            check()
            return ET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        root_rels = xml("_rels/.rels")
        content_types = xml("[Content_Types].xml")
        if any("macroenabled" in e.get("ContentType", "").lower() or "vbaproject" in e.get("ContentType", "").lower() for e in content_types):
            raise Refused("MACRO_PACKAGE_UNSUPPORTED")
        candidates = [e for e in root_rels if e.tag == R + "Relationship" and e.get("Type", "").endswith("/officeDocument")]
        if len(candidates) != 1 or candidates[0].get("TargetMode") == "External":
            raise Refused("INVALID_DOCX_RELATIONSHIPS")
        part = candidates[0].get("Target", "").lstrip("/")
        if part not in names:
            raise Refused("INVALID_DOCX_MAIN_PART")
        main_types = [e.get("ContentType") for e in content_types if e.get("PartName", "").lstrip("/") == part]
        if main_types != ["application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"]:
            raise Refused("UNSUPPORTED_DOCX_XML")
        style_parts = {e.get("PartName", "").lstrip("/") for e in content_types if "styles" in e.get("ContentType", "").lower()}
        document = xml(part)
        if document.tag != W + "document" or document.find(W + "body") is None:
            raise Refused("UNSUPPORTED_DOCX_XML")
        for name in names:
            lower = name.lower()
            for fragment, flag in (("header", "headers"), ("footer", "footers"), ("comments", "comments"),
                ("footnotes", "footnotes"), ("endnotes", "endnotes"), ("media/", "images"),
                ("embeddings/", "embedded_objects"), ("customxml/", "custom_xml"), ("vbaproject", "macros")):
                if fragment in lower:
                    unprocessed.add(flag)
            if lower.endswith(".rels"):
                for rel in xml(name):
                    if "vbaproject" in rel.get("Type", "").lower():
                        unprocessed.add("macros")
                    if rel.get("TargetMode") == "External":
                        unprocessed.add("external_relationship_targets")
            if lower.endswith("styles.xml") or name in style_parts:
                styles = xml(name)
                if any(node.tag in {W + "vanish", W + "webHidden"} for node in styles.iter()):
                    unprocessed.add("hidden_style_text")
                    complete = False
        if "macros" in unprocessed:
            raise Refused("MACRO_PACKAGE_UNSUPPORTED")
        def paragraph_text(element):
            nonlocal complete
            forbidden = {"ins": "revisions", "del": "revisions", "moveFrom": "revisions", "moveTo": "revisions",
                         "fldSimple": "fields", "fldChar": "fields", "instrText": "fields",
                         "vanish": "hidden_text", "webHidden": "hidden_text", "sdt": "content_controls",
                         "txbxContent": "text_boxes", "altChunk": "external_content", "sym": "symbols",
                         "ptab": "positioned_tabs"}
            text, flags = [], set()
            for node in element.iter():
                check()
                if node.tag.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/math}"):
                    flags.add("math")
                if node.tag.startswith(W):
                    local = node.tag[len(W):]
                    if local in forbidden:
                        flags.add(forbidden[local])
                    elif local == "t":
                        text.append(node.text or "")
                    elif local == "tab":
                        text.append("\t")
                    elif local in {"br", "cr"}:
                        text.append("\n")
                    elif local == "noBreakHyphen":
                        text.append("\u2011")
                    elif local == "softHyphen":
                        text.append("\u00ad")
                    elif local in {"drawing", "pict", "object"}:
                        flags.add("images_or_objects")
                    elif local in {"hyperlink", "bookmarkStart", "bookmarkEnd"}:
                        unprocessed.add("hyperlinks_or_bookmarks")
            unprocessed.update(flags)
            if flags - {"images_or_objects"} or flags and any(text):
                complete = False
                return None
            return "".join(text)
        total_chars = 0
        def append(text, **metadata):
            nonlocal total_chars
            if text is not None:
                total_chars += len(text) + 1
                units.append({"text": text, "part": part, **metadata})
                if len(units) > limits["max_blocks"] or total_chars > limits["max_text_chars"] or metadata["kind"] == "docx_cell" and len(text) > limits["max_cell_chars"]:
                    raise Refused("CONTAINER_TEXT_LIMIT")
        paragraph, table = 0, 0
        for child in document.find(W + "body"):
            check()
            if child.tag == W + "p":
                append(paragraph_text(child), kind="docx_paragraph", paragraph=paragraph)
                paragraph += 1
            elif child.tag == W + "tbl":
                if any(e.tag in {W + "vMerge", W + "gridSpan", W + "gridBefore", W + "gridAfter"} for e in child.iter()) or len(list(child.iter(W + "tbl"))) != 1:
                    unprocessed.add("merged_or_nested_tables")
                    complete = False
                    table += 1
                    continue
                if any(e.tag in {W + "ins", W + "del", W + "moveFrom", W + "moveTo", W + "sdt"} for e in child.iter()):
                    unprocessed.add("revisions_or_controls_in_tables")
                    complete = False
                    table += 1
                    continue
                for row_index, row in enumerate(child.findall(W + "tr")):
                    if row_index >= limits["max_csv_rows"]:
                        raise Refused("DOCX_TABLE_LIMIT")
                    for col_index, cell in enumerate(row.findall(W + "tc")):
                        if col_index >= limits["max_csv_columns"]:
                            raise Refused("DOCX_TABLE_LIMIT")
                        values = [paragraph_text(p) for p in cell.findall(W + "p")]
                        append("\n".join(values) if all(v is not None for v in values) else None,
                               kind="docx_cell", table=table, row=row_index, column=col_index)
                table += 1
            elif child.tag != W + "sectPr":
                unprocessed.add("unhandled_body_content")
                complete = False
        return {"units": units, "unprocessed": sorted(unprocessed), "projection_complete": complete,
                "total_parts": table, "scope": "docx_selected_text"}


def pdf(payload, limits, check):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(payload), strict=True)
    if reader.is_encrypted:
        raise Refused("PDF_ENCRYPTED")
    if not 0 < len(reader.pages) <= limits["max_pdf_pages"]:
        raise Refused("PDF_PAGE_LIMIT")
    units, unprocessed = [], {"pdf_metadata", "visual_layout_not_verified"}
    complete, total = True, 0
    root = reader.trailer["/Root"]
    for key, flag in (("/AcroForm", "pdf_forms"), ("/Outlines", "pdf_bookmarks"), ("/Names", "pdf_names_or_attachments"), ("/OpenAction", "pdf_actions")):
        if key in root:
            unprocessed.add(flag)
    for index, page in enumerate(reader.pages, 1):
        check()
        contents = page.get_contents()
        if contents is not None:
            size = len(contents.get_data())
            total += size
            if size > limits["max_file_bytes"] or total > limits["max_container_bytes"]:
                raise Refused("PDF_STREAM_LIMIT")
        if "/Annots" in page:
            unprocessed.add("pdf_annotations")
        resources = page.get("/Resources", {})
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        if resources.get("/XObject"):
            unprocessed.add("pdf_images_or_xobjects")
        text = page.extract_text(extraction_mode="plain")
        check()
        if not text.strip():
            unprocessed.add("empty_or_image_only_pages")
        if "\ufffd" in text or "\x00" in text:
            unprocessed.add("unresolved_text_encoding")
            complete = False
        units.append({"kind": "pdf_page", "text": text, "page": index, "part": "page"})
        if sum(len(u["text"]) for u in units) > limits["max_text_chars"]:
            raise Refused("CONTAINER_TEXT_LIMIT")
    if not any(u["text"].strip() for u in units):
        raise Refused("PDF_NO_TEXT")
    return {"units": units, "unprocessed": sorted(unprocessed), "projection_complete": complete,
            "total_parts": len(units), "scope": "pdf_selected_page_text"}


def main():
    logging.disable(logging.CRITICAL)
    kind = sys.argv[1]
    limits = json.loads(sys.argv[2])
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limits["max_worker_memory_bytes"], limits["max_worker_memory_bytes"]))
    deadline = time.monotonic() + limits["max_seconds"]
    def check():
        if time.monotonic() >= deadline:
            raise Refused("CONTAINER_TIME_LIMIT")
    try:
        payload = sys.stdin.buffer.read(limits["max_file_bytes"] + 1)
        if len(payload) > limits["max_file_bytes"]:
            raise Refused("FILE_LIMIT")
        result = docx(payload, limits, check) if kind == "docx" else pdf(payload, limits, check)
        output = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(output) > 32 * 1024 * 1024:
            raise Refused("CONTAINER_RESULT_LIMIT")
    except Refused as exc:
        output = json.dumps({"error": str(exc)}).encode()
    except MemoryError:
        output = b'{"error":"CONTAINER_MEMORY_LIMIT"}'
    except Exception:
        output = b'{"error":"CONTAINER_PARSE_FAILED"}'
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
