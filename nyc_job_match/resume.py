"""Parse a resume entirely in memory; PDF OCR is sent to Mistral.

TXT, Markdown and DOCX extraction happen locally.  This module never writes
resume bytes or extracted text to a file, and returns only a cleaned basename.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .clients import ServiceError, Settings, request_json


MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARACTERS = 20_000
MAX_DOCX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_DOCX_XML_BYTES = 1024 * 1024
MAX_BASE64_CHARACTERS = ((MAX_FILE_BYTES + 2) // 3) * 4
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}
OCR_URL = "https://api.mistral.ai/v1/ocr"
WORD_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}


class ResumeError(ValueError):
    """A resume cannot be accepted or safely parsed."""


def _clean_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise ResumeError("Select a resume file.")
    # Browsers normally supply a basename, but callers can send a Windows or
    # POSIX path.  No directory information is returned to the UI.
    name = Path(filename.replace("\\", "/")).name
    name = "".join(character for character in name if ord(character) >= 32 and ord(character) != 127).strip()
    if not name or name in {".", ".."}:
        raise ResumeError("Invalid resume filename. Select the file again.")
    return name


def _decode_content(content_base64: str) -> bytes:
    if not isinstance(content_base64, str):
        raise ResumeError("Invalid resume upload format. Select the file again.")
    if len(content_base64) > MAX_BASE64_CHARACTERS:
        raise ResumeError("Resume files must not exceed 5 MB. Upload a smaller file.")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ResumeError("The resume upload is not valid Base64. Select the file again.") from None
    if not content:
        raise ResumeError("The resume file is empty. Upload a resume containing text.")
    if len(content) > MAX_FILE_BYTES:
        raise ResumeError("Resume files must not exceed 5 MB. Upload a smaller file.")
    return content


def _validated_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ResumeError("No readable text was found. Upload a resume containing text.")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ResumeError("Resume text must not exceed 20,000 characters. Upload a shorter resume or reduce its content.")
    return text


def _parse_text(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ResumeError("TXT and Markdown resumes must use UTF-8 encoding. Save as UTF-8 and try again.") from None
    if re.search(r"[\x00-\x08\x0b\x0e-\x1f\x7f]", text):
        raise ResumeError("The TXT or Markdown file contains binary data. Upload plain UTF-8 text.")
    if content.startswith((b"%PDF", b"PK\x03\x04")):
        raise ResumeError("The file content does not match its TXT or Markdown extension. Use the correct file format.")
    return text


def _parse_pdf(content: bytes, settings: Settings) -> str:
    if not content.startswith(b"%PDF-"):
        raise ResumeError("The file has no valid PDF signature. Upload a valid PDF resume.")
    if not settings.mistral_configured:
        raise ResumeError("PDF resumes require Mistral OCR. Configure a Mistral API key first, or upload TXT, Markdown, or DOCX.")
    encoded = base64.b64encode(content).decode("ascii")
    try:
        result = request_json(
            OCR_URL,
            method="POST",
            headers={
                "Authorization": "Bearer " + settings.mistral_api_key,
                "Content-Type": "application/json",
            },
            body={
                "model": "mistral-ocr-latest",
                "document": {"type": "document_url", "document_url": "data:application/pdf;base64," + encoded},
                "include_image_base64": False,
            },
            timeout=60,
        )
    except ServiceError:
        # Provider error messages must not accidentally echo resume content,
        # request headers or credentials back to the browser.
        raise ResumeError("The Mistral OCR request failed. Check your key, account credits, and connection before retrying.") from None
    if not isinstance(result, dict) or not isinstance(result.get("pages"), list):
        raise ResumeError("Mistral OCR returned an invalid document result. Please try again later.")
    markdown_pages = []
    for page in result["pages"]:
        if not isinstance(page, dict) or not isinstance(page.get("markdown"), str):
            raise ResumeError("Mistral OCR returned invalid page text. Please try again later.")
        markdown_pages.append(page["markdown"])
    return "\n\n".join(markdown_pages)


def _parse_docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 10_000 or sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ResumeError("An expanded DOCX must not exceed 20 MB. Upload a smaller resume.")
            documents = [entry for entry in entries if entry.filename == "word/document.xml"]
            if len(documents) != 1:
                raise ResumeError("The DOCX has no valid document body. Export the Word resume again.")
            document = documents[0]
            if document.file_size > MAX_DOCX_XML_BYTES:
                raise ResumeError("The DOCX body XML must not exceed 1 MB. Upload a shorter resume.")
            if document.flag_bits & 1:
                raise ResumeError("Encrypted DOCX files are not supported. Remove the password and try again.")
            with archive.open(document) as handle:
                xml_content = handle.read(MAX_DOCX_XML_BYTES + 1)
            if len(xml_content) > MAX_DOCX_XML_BYTES:
                raise ResumeError("The DOCX body XML must not exceed 1 MB. Upload a shorter resume.")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, NotImplementedError):
        raise ResumeError("Could not read the DOCX resume. Check that the file is complete and unencrypted.") from None
    # Resume documents do not need a DTD.  Rejecting declarations avoids XML
    # entity expansion, and no external content or ZIP entries are extracted.
    # XML may use UTF-16/32; removing NULs for this scan also catches encoded
    # declarations without changing the bytes passed to the XML parser.
    declaration_scan = xml_content.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise ResumeError("The DOCX body contains unsupported XML declarations. Export the resume again.")
    try:
        root = ElementTree.fromstring(xml_content)
    except ElementTree.ParseError:
        raise ResumeError("Invalid DOCX body XML. Export the Word resume again.") from None
    parts: list[str] = []

    def collect(node: ElementTree.Element) -> None:
        tag = node.tag
        namespace, _, name = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
        word_tag = namespace in WORD_NAMESPACES
        if word_tag and name == "t":
            parts.append(node.text or "")
        elif word_tag and name == "tab":
            parts.append("\t")
        elif word_tag and name in {"br", "cr"}:
            parts.append("\n")
        else:
            for child in node:
                collect(child)
        if word_tag and name == "p":
            parts.append("\n")

    try:
        collect(root)
    except RecursionError:
        raise ResumeError("The DOCX body is too complex. Export a simpler resume.") from None
    return "".join(parts)


def parse_resume(filename: str, content_base64: str, settings: Settings) -> dict[str, Any]:
    """Return ``{filename, text, parser}`` without saving the uploaded resume.

    PDF bytes are sent to Mistral OCR; the caller must explain this upload to
    the user before invoking the parser.  Other supported formats are local.
    """
    filename = _clean_filename(filename)
    extension = Path(filename).suffix.casefold()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ResumeError("Supported resume formats are PDF, TXT, Markdown (.md), and DOCX. Select one of these formats.")
    content = _decode_content(content_base64)
    if extension in {".txt", ".md"}:
        text, parser = _parse_text(content), "utf-8"
    elif extension == ".docx":
        text, parser = _parse_docx(content), "docx-xml"
    else:
        text, parser = _parse_pdf(content, settings), "mistral-ocr"
    return {"filename": filename, "text": _validated_text(text), "parser": parser}
