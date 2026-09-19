"""Embedded-metadata scrubbing for mirrored copies.

Only meaningful for real copies -- hardlinks share bytes with the originals,
so scrubbing must force copy mode. Covers the formats that leak author /
device info most often:

- Office (docx/xlsx/pptx): docProps creator, lastModifiedBy, company, etc.
- PDF: /Info dict + XMP packet
- JPEG: EXIF (GPS, camera, artist) via piexif (no re-encode)

Anything unrecognized or unparsable is copied byte-identical and counted as
unscrubbed -- never block the mirror on one weird file.
"""

from __future__ import annotations

import shutil
import os
import zipfile
from pathlib import Path

from ..scanner import winlong

# fixed timestamp for scrubbed mirrors: 2000-01-01T00:00:00
EPOCH = 946684800

_OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
_JPEG_EXTS = {".jpg", ".jpeg"}

# Namespace URIs, not arbitrary XML prefixes, identify Office fields.
_DC = "http://purl.org/dc/elements/1.1/"
_CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_APP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_CORE_FIELDS = (
    *[f"{{{_DC}}}{name}" for name in ("title", "subject", "creator", "description")],
    *[f"{{{_CP}}}{name}" for name in ("keywords", "lastModifiedBy", "revision")],
)
_APP_FIELDS = tuple(f"{{{_APP}}}{name}" for name in ("Company", "Manager", "HyperlinkBase"))


def _clear_xml_fields(xml: bytes, fields: tuple[str, ...], *, custom=False) -> bytes:
    from defusedxml.minidom import parseString

    if len(xml) > 4 * 1024 * 1024:
        raise ValueError("metadata part exceeds supported size")
    document = parseString(xml, forbid_dtd=True)
    root = document.documentElement
    expected = (f"{{{_CUSTOM}}}Properties" if custom else
                f"{{{_CP}}}coreProperties" if fields == _CORE_FIELDS else f"{{{_APP}}}Properties")
    def qname(element):
        return f"{{{element.namespaceURI}}}{element.localName}"
    try:
        if qname(root) != expected:
            raise ValueError("unsupported metadata namespace")
        for element in list(document.getElementsByTagName("*")):
            if qname(element) in fields:
                for child in list(element.childNodes):
                    element.removeChild(child).unlink()
                # Preserve namespace declarations: a prefix may be declared
                # on this element itself. Other attributes can contain PII.
                for attribute in list(element.attributes.values()):
                    if attribute.namespaceURI != "http://www.w3.org/2000/xmlns/":
                        element.removeAttributeNode(attribute)
            elif custom and qname(element) == f"{{{_CUSTOM}}}property":
                element.parentNode.removeChild(element).unlink()
        # DOM serialization preserves prefixes used in xsi:type and other
        # QName-valued attributes, unlike automatic namespace renumbering.
        output = document.toxml(encoding="utf-8")
    finally:
        document.unlink()
    checked = parseString(output, forbid_dtd=True)
    try:
        for element in checked.getElementsByTagName("*"):
            attributes = [a for a in element.attributes.values()
                          if a.namespaceURI != "http://www.w3.org/2000/xmlns/"]
            if qname(element) in fields and (element.childNodes or attributes):
                raise ValueError("metadata verification failed")
            if custom and qname(element) == f"{{{_CUSTOM}}}property":
                raise ValueError("custom metadata verification failed")
    finally:
        checked.unlink()
    return output


def scrub_office(src: str, dst: str) -> None:
    with zipfile.ZipFile(src, "r") as zin, \
         zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            name = item.filename.replace("\\", "/")
            if name in {"docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"} and item.file_size > 4 * 1024 * 1024:
                raise ValueError("metadata part exceeds supported size")
            if name == "docProps/core.xml":
                data = _clear_xml_fields(zin.read(item), _CORE_FIELDS)
            elif name == "docProps/app.xml":
                data = _clear_xml_fields(zin.read(item), _APP_FIELDS)
            elif name == "docProps/custom.xml":
                data = _clear_xml_fields(zin.read(item), (), custom=True)
            else:
                with zin.open(item) as source, zout.open(item, "w") as target:
                    shutil.copyfileobj(source, target)
                continue
            zout.writestr(item, data)


def scrub_pdf(src: str, dst: str) -> None:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(src, strict=True)
    writer = PdfWriter(clone_from=reader)
    if writer.metadata is not None:
        writer.metadata.clear()
    writer.metadata = None
    writer.xmp_metadata = None
    writer.compress_identical_objects(remove_unreferenced=True)
    with open(dst, "wb") as f:
        writer.write(f)
    checked = PdfReader(dst)
    if checked.metadata or checked.xmp_metadata or len(checked.pages) != len(reader.pages):
        raise ValueError("PDF metadata verification failed")


def scrub_jpeg(src: str, dst: str) -> None:
    import piexif

    piexif.remove(src, dst)
    exif = piexif.load(dst)
    if any(exif.get(key) for key in ("0th", "Exif", "GPS", "Interop", "1st", "thumbnail")):
        raise ValueError("JPEG EXIF verification failed")


_SCRUBBERS = {
    **{e: scrub_office for e in _OFFICE_EXTS},
    ".pdf": scrub_pdf,
    **{e: scrub_jpeg for e in _JPEG_EXTS},
}


def scrubbable(path: Path) -> bool:
    return path.suffix.lower() in _SCRUBBERS


def scrub_copy(src: Path, dst: Path) -> bool:
    """Copy src to dst, stripping embedded metadata when the format is
    supported. Returns True if metadata scrubbing was actually applied."""
    if src.resolve() == dst.resolve() or (dst.exists() and os.path.samefile(winlong(src), winlong(dst))):
        raise ValueError("scrub source and destination are the same file")
    scrubber = _SCRUBBERS.get(src.suffix.lower())
    if scrubber is None:
        shutil.copyfile(winlong(src), winlong(dst))
        return False
    try:
        scrubber(winlong(src), winlong(dst))
    except Exception:
        # corrupt or odd file: fall back to a plain copy, never fail the run
        shutil.copyfile(winlong(src), winlong(dst))
        return False
    return True


def freeze_times(path: Path, *, strict: bool = False) -> None:
    """Flatten mtime/atime -- timestamps leak timezone and work patterns."""
    import os

    try:
        os.utime(winlong(path), (EPOCH, EPOCH))
    except OSError:
        if strict:
            raise
